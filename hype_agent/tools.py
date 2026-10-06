"""Data-gathering tools the hype agent can call."""

import json
import os
import statistics
import time
from datetime import date, datetime, timedelta, timezone

import httpx
from anthropic import beta_tool

from hype_agent import storage

POKEMON_TCG_API = "https://api.pokemontcg.io/v2"
REDDIT_SEARCH = "https://www.reddit.com/r/{subreddit}/search.json"
TWITTERAPI_SEARCH = "https://api.twitterapi.io/twitter/tweet/advanced_search"
X_PROVIDER = "twitterapi.io"
DEFAULT_X_DAILY_TWEET_CAP = 1500
USER_AGENT = "pokemon-card-hype-agent/0.1"
TIMEOUT = 15.0


def _tcg_headers() -> dict[str, str]:
    headers = {"User-Agent": USER_AGENT}
    if key := os.environ.get("POKEMONTCG_API_KEY"):
        headers["X-Api-Key"] = key
    return headers


def tcg_get(path: str, params: dict, attempts: int = 6) -> dict:
    """GET from the Pokémon TCG API, retrying its frequent transient 5xx errors."""
    for attempt in range(attempts):
        resp = httpx.get(f"{POKEMON_TCG_API}/{path}", params=params, headers=_tcg_headers(), timeout=60.0)
        if resp.status_code < 500 or attempt == attempts - 1:
            resp.raise_for_status()
            return resp.json()
        time.sleep(2 * (attempt + 1))
    raise AssertionError("unreachable")


@beta_tool
def search_cards(query: str, limit: int = 10) -> str:
    """Search Pokémon TCG cards and return their current TCGplayer market prices.

    Args:
        query: Pokémon TCG API query, e.g. 'name:charizard' or 'set.id:sv8 rarity:"Special Illustration Rare"'.
        limit: Maximum number of cards to return (1-50).
    """
    limit = max(1, min(limit, 50))
    body = tcg_get("cards", {"q": query, "pageSize": limit, "orderBy": "-set.releaseDate"})
    cards = []
    for card in body.get("data", []):
        tcgplayer = card.get("tcgplayer", {})
        cards.append(
            {
                "id": card["id"],
                "name": card["name"],
                "set": card.get("set", {}).get("name"),
                "release_date": card.get("set", {}).get("releaseDate"),
                "rarity": card.get("rarity"),
                "prices": tcgplayer.get("prices", {}),
                "prices_updated": tcgplayer.get("updatedAt"),
            }
        )
    with storage.connect() as conn:
        for card in cards:
            storage.record_prices(conn, card)
    return json.dumps(cards)


@beta_tool
def reddit_buzz(query: str, subreddit: str = "PokemonTCG", limit: int = 15) -> str:
    """Find recent Reddit posts about a card or set to gauge community hype.

    Args:
        query: Search terms, e.g. 'Umbreon ex' or 'Prismatic Evolutions'.
        subreddit: Subreddit to search, e.g. 'PokemonTCG' or 'PokeInvesting'.
        limit: Maximum number of posts to return (1-50).
    """
    limit = max(1, min(limit, 50))
    resp = httpx.get(
        REDDIT_SEARCH.format(subreddit=subreddit),
        params={"q": query, "restrict_sr": 1, "sort": "new", "t": "week", "limit": limit},
        headers={"User-Agent": USER_AGENT},
        timeout=TIMEOUT,
        follow_redirects=True,
    )
    resp.raise_for_status()
    children = [c["data"] for c in resp.json().get("data", {}).get("children", [])]
    posts = [
        {
            "title": p["title"],
            "score": p["score"],
            "comments": p["num_comments"],
            "created_utc": p["created_utc"],
            "url": "https://reddit.com" + p["permalink"],
        }
        for p in children
    ]
    with storage.connect() as conn:
        storage.record_posts(
            conn,
            f"reddit/{subreddit.lower()}",
            query,
            [
                {
                    "id": p["id"],
                    "author": p.get("author"),
                    "engagement": p["score"] + p["num_comments"],
                    "created_date": datetime.fromtimestamp(p["created_utc"], timezone.utc).date().isoformat(),
                }
                for p in children
            ],
        )
    return json.dumps(posts)


def _x_daily_cap() -> int:
    return int(os.environ.get("X_DAILY_TWEET_CAP", DEFAULT_X_DAILY_TWEET_CAP))


def _parse_x_date(value: str | None) -> datetime | None:
    if not value:
        return None
    for fmt in ("%a %b %d %H:%M:%S %z %Y", "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.strptime(value.replace("Z", "+0000"), fmt)
        except ValueError:
            pass
    return None


def tweet_engagement(t: dict) -> int:
    return sum(t.get(k) or 0 for k in ("likeCount", "retweetCount", "replyCount", "quoteCount"))


def tweet_record(t: dict) -> dict:
    """The fields storage.record_posts keeps for a tweet."""
    return {
        "id": t.get("id"),
        "author": (t.get("author") or {}).get("userName"),
        "author_followers": (t.get("author") or {}).get("followers"),
        "engagement": tweet_engagement(t),
        "created_date": (_parse_x_date(t.get("createdAt")) or datetime.now(timezone.utc)).date().isoformat(),
    }


def fetch_x(conn, key: str, query: str, max_tweets: int, sort: str = "Latest") -> tuple[list[dict], bool]:
    """Page through an X search until max_tweets. Returns (tweets, more_available).

    Every page is billed to today's usage as soon as it arrives, so a crash can't hide spend.
    """
    tweets: dict[str, dict] = {}
    cursor = ""
    more = False
    while len(tweets) < max_tweets:
        resp = httpx.get(
            TWITTERAPI_SEARCH,
            params={"query": query, "queryType": sort, "cursor": cursor},
            headers={"X-API-Key": key, "User-Agent": USER_AGENT},
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        page = data.get("tweets") or []
        storage.add_usage(conn, X_PROVIDER, len(page))
        conn.commit()
        before = len(tweets)
        for t in page:
            tweets.setdefault(str(t.get("id")), t)
        more = bool(data.get("has_next_page") and data.get("next_cursor"))
        # Stop when there are no more pages or a page brings nothing new (repeated cursor).
        if len(tweets) == before:
            more = False
        if not more:
            break
        cursor = data["next_cursor"]
    found = list(tweets.values())
    return found[:max_tweets], more or len(found) > max_tweets


def _summarize_tweets(tweets: list[dict]) -> dict:
    now = datetime.now(timezone.utc)
    authors: dict[str, dict] = {}
    for t in tweets:
        author = t.get("author") or {}
        authors.setdefault(author.get("userName") or "?", author)
    new_accounts = 0
    for a in authors.values():
        created = _parse_x_date(a.get("createdAt"))
        if created and (now - created).days < 90:
            new_accounts += 1

    top = sorted(tweets, key=tweet_engagement, reverse=True)[:5]
    followers = [a.get("followers") or 0 for a in authors.values()]
    return {
        "tweets": len(tweets),
        "unique_authors": len(authors),
        "likes": sum(t.get("likeCount") or 0 for t in tweets),
        "retweets": sum(t.get("retweetCount") or 0 for t in tweets),
        "replies": sum(t.get("replyCount") or 0 for t in tweets),
        "views": sum(t.get("viewCount") or 0 for t in tweets),
        "median_author_followers": statistics.median(followers) if followers else 0,
        "authors_with_accounts_under_90_days": new_accounts,
        "top_tweets": [
            {
                "text": (t.get("text") or "")[:280],
                "url": t.get("url"),
                "author": (t.get("author") or {}).get("userName"),
                "author_followers": (t.get("author") or {}).get("followers"),
                "likes": t.get("likeCount"),
                "retweets": t.get("retweetCount"),
                "created_at": t.get("createdAt"),
            }
            for t in top
        ],
    }


@beta_tool
def x_buzz(query: str, max_tweets: int = 40, sort: str = "Latest") -> str:
    """Search recent X (Twitter) posts about a card or set to gauge hype, and save today's counts.

    Returns engagement totals, how many different people are posting, a bot-risk hint
    (authors with brand-new accounts), the top tweets, and the daily mention history stored so far.
    Retweets are excluded. Each tweet returned costs API credits, so keep max_tweets modest.

    Args:
        query: X search query, e.g. '"Umbreon ex" PSA' or '"Prismatic Evolutions"'. X search operators work.
        max_tweets: Maximum tweets to fetch (1-100). Fetched 20 per page.
        sort: 'Latest' for the newest tweets or 'Top' for the most engaged.
    """
    key = os.environ.get("TWITTERAPI_IO_KEY")
    if not key:
        return json.dumps({"error": "X search is not configured (TWITTERAPI_IO_KEY is not set)."})
    max_tweets = max(1, min(max_tweets, 100))
    sort = "Top" if sort.lower() == "top" else "Latest"

    with storage.connect() as conn:
        remaining = _x_daily_cap() - storage.usage_today(conn, X_PROVIDER)
        if remaining <= 0:
            return json.dumps({"error": f"Daily X tweet cap of {_x_daily_cap()} reached; try again tomorrow."})

        found, _ = fetch_x(conn, key, f"{query} -filter:retweets", min(max_tweets, remaining), sort)
        storage.record_posts(conn, "x", query, [tweet_record(t) for t in found])
        result = _summarize_tweets(found)
        result["daily_history"] = storage.social_history(conn, query)
        result["tweets_used_today"] = storage.usage_today(conn, X_PROVIDER)
        result["daily_cap"] = _x_daily_cap()
    return json.dumps(result)


@beta_tool
def hype_history(subject: str, card_name: str = "", days: int = 30) -> str:
    """Look up stored history to spot momentum: daily mention counts per platform and daily prices.

    History only exists for searches run on earlier days, so it starts empty and grows each day.

    Args:
        subject: The exact search query used earlier with x_buzz or reddit_buzz, e.g. 'Umbreon ex'.
        card_name: Card name to look up stored prices for, e.g. 'Umbreon ex'. Defaults to subject.
        days: How many days back to look (1-365).
    """
    days = max(1, min(days, 365))
    with storage.connect() as conn:
        return json.dumps(
            {
                "as_of": date.today().isoformat(),
                "social": storage.social_history(conn, subject, days),
                "prices": storage.price_history(conn, card_name or subject, days),
            }
        )


def _leaderboard_rows(totals: dict, previous: dict, prev_days: int, days: int) -> list[dict]:
    rows = []
    for subject, t in totals.items():
        prev = previous.get(subject)
        # Compare like with like: the previous window's average per day, scaled to this window's length.
        enough = prev and prev["days"] >= max(1, prev_days // 2)
        baseline = prev["est_unique_authors"] / prev["days"] * days if enough else None
        rows.append(
            {
                "subject": subject,
                "est_posts": round(t["est_posts"], 1),
                "est_unique_authors": round(t["est_unique_authors"], 1),
                "tweets_sampled": t["sampled"],
                "days_hitting_sample_cap": t["capped_days"],
                "sampled_engagement": t["engagement"],
                "top_author_share_of_sample": round(t["top_author_posts"] / t["sampled"], 2) if t["sampled"] else 0,
                "previous_est_unique_authors": round(baseline, 1) if baseline is not None else None,
                "change_pct": round((t["est_unique_authors"] - baseline) / max(baseline, 1) * 100)
                if baseline is not None else None,
            }
        )
    return rows


@beta_tool
def hype_leaderboard(period: str = "day", sort: str = "volume", limit: int = 5) -> str:
    """Rank watchlist cards by how much people posted about them on X, from the daily collection job.

    Use this for broad questions like "hottest cards today", "top 5 this week" or "what's rising".
    Data covers only cards on the watchlist and only full UTC days already collected.
    Volumes on busy days are estimated from a sample (see est_posts / days_hitting_sample_cap).

    Args:
        period: 'day' for the latest collected day, or 'week' for the last 7 collected days.
        sort: 'volume' for the most-discussed cards (by estimated unique authors), 'rising' for the biggest
            increase in unique authors vs. the previous period (the 7 days before, for 'day').
        limit: How many cards to return (1-20).
    """
    limit = max(1, min(limit, 20))
    days = 7 if period.lower() == "week" else 1
    with storage.connect() as conn:
        latest = storage.latest_estimate_date(conn, "x")
        if not latest:
            return json.dumps({"error": "No watchlist data collected yet; the daily collection job has not run."})
        end = date.fromisoformat(latest)
        start = end - timedelta(days=days - 1)
        # 'day' compares with the 7 days before it; 'week' with the week before it.
        prev_days = 7
        prev_end = start - timedelta(days=1)
        prev_start = prev_end - timedelta(days=prev_days - 1)
        totals = storage.estimate_totals(conn, "x", start.isoformat(), end.isoformat())
        previous = storage.estimate_totals(conn, "x", prev_start.isoformat(), prev_end.isoformat())
        rows = _leaderboard_rows(totals, previous, prev_days, days)
        if sort.lower() == "rising":
            # Require several different people so one account posting a lot, or a jump from 1 to 3, can't top it.
            rows = [r for r in rows if r["change_pct"] is not None and r["est_unique_authors"] >= 5 * days]
            rows.sort(key=lambda r: r["change_pct"], reverse=True)
        else:
            rows.sort(key=lambda r: r["est_unique_authors"], reverse=True)
        rows = rows[:limit]
        for r in rows:
            r["priciest_printings"] = storage.latest_prices(conn, r["subject"])
        days_collected = conn.execute(
            "SELECT COUNT(DISTINCT date) FROM social_estimates WHERE platform = 'x'"
        ).fetchone()[0]
    return json.dumps(
        {
            "window": {"start": start.isoformat(), "end": end.isoformat(), "timezone": "UTC"},
            "compared_with": {"start": prev_start.isoformat(), "end": prev_end.isoformat()},
            "sort": "rising" if sort.lower() == "rising" else "volume (estimated unique authors)",
            "watchlist_subjects_with_data": len(totals),
            "days_collected_total": days_collected,
            "cards": rows,
            "note": "Only watchlist cards are ranked. est_* values on capped days are extrapolated from the "
            "newest tweets' posting rate. Giveaway tweets and retweets are excluded at search time.",
        }
    )


ALL_TOOLS = [search_cards, reddit_buzz, x_buzz, hype_history, hype_leaderboard]

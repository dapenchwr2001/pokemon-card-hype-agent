"""Data-gathering tools the hype agent can call."""

import json
import os
import statistics
from datetime import date, datetime, timezone

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


@beta_tool
def search_cards(query: str, limit: int = 10) -> str:
    """Search Pokémon TCG cards and return their current TCGplayer market prices.

    Args:
        query: Pokémon TCG API query, e.g. 'name:charizard' or 'set.id:sv8 rarity:"Special Illustration Rare"'.
        limit: Maximum number of cards to return (1-50).
    """
    limit = max(1, min(limit, 50))
    resp = httpx.get(
        f"{POKEMON_TCG_API}/cards",
        params={"q": query, "pageSize": limit, "orderBy": "-set.releaseDate"},
        headers=_tcg_headers(),
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    cards = []
    for card in resp.json().get("data", []):
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

    def engagement(t: dict) -> int:
        return sum(t.get(k) or 0 for k in ("likeCount", "retweetCount", "replyCount", "quoteCount"))

    top = sorted(tweets, key=engagement, reverse=True)[:5]
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

        tweets: dict[str, dict] = {}
        cursor = ""
        while len(tweets) < min(max_tweets, remaining):
            resp = httpx.get(
                TWITTERAPI_SEARCH,
                params={"query": f"{query} -filter:retweets", "queryType": sort, "cursor": cursor},
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
            # Stop when there are no more pages or a page brings nothing new (repeated cursor).
            if not data.get("has_next_page") or not data.get("next_cursor") or len(tweets) == before:
                break
            cursor = data["next_cursor"]

        found = list(tweets.values())[:max_tweets]
        storage.record_posts(
            conn,
            "x",
            query,
            [
                {
                    "id": t.get("id"),
                    "author": (t.get("author") or {}).get("userName"),
                    "author_followers": (t.get("author") or {}).get("followers"),
                    "engagement": sum(t.get(k) or 0 for k in ("likeCount", "retweetCount", "replyCount", "quoteCount")),
                    "created_date": (_parse_x_date(t.get("createdAt")) or datetime.now(timezone.utc)).date().isoformat(),
                }
                for t in found
            ],
        )
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


ALL_TOOLS = [search_cards, reddit_buzz, x_buzz, hype_history]

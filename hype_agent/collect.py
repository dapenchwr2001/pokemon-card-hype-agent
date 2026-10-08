"""Daily collection job: measure X volume for every watchlist card, one full UTC day at a time.

Run once a day shortly after midnight UTC:

    python -m hype_agent.collect

On the first run it backfills the last --backfill-days days. Each later run collects whichever
full days are still missing, newest first, until the run's tweet budget is spent; days it can't
afford stay missing and are picked up next time. The watchlist lives in a JSON file you can edit.
"""

import argparse
import json
import os
import re
import time
from collections import Counter
from datetime import date, datetime, timedelta, timezone

from hype_agent import storage, tools

PLATFORM = "x"
DEFAULT_PER_DAY_CAP = 40  # TwitterAPI.io returns 20 tweets per page, so keep this a multiple of 20.
DISCOVERY_EVERY_DAYS = 7
DISCOVERY_TWEETS = 200
DISCOVERY_MIN_AUTHORS = 3
PRICE_TIME_LIMIT_S = 240  # The TCG API is often slow or failing; prices are best effort, so cap the time spent.

# Always tracked, whatever the recent sets are.
EVERGREEN = ["Charizard ex", "Umbreon ex", "Pikachu ex", "Mew ex", "Gengar ex", "Rayquaza ex", "Sylveon ex", "Lugia ex"]
SEED_SETS = 4

# Species-level and promo searches catch what "<name> ex" misses: older Pokémon, non-ex printings, vintage,
# promos. Species need card words next to the name or every game/anime tweet would count. Edit freely.
EXTRA_CAP = 20  # One page per day; these are cheap probes, not full counts.
CARD_WORDS = '("pokemon card" OR "pokemon tcg" OR #PokemonTCG OR PSA OR pulled OR binder)'
SPECIES = [
    "Aerodactyl", "Beedrill", "Kabutops", "Omastar", "Dragonite", "Gyarados", "Alakazam", "Machamp",
    "Arcanine", "Ninetales", "Scyther", "Pinsir", "Kangaskhan", "Lapras", "Snorlax", "Ditto", "Venusaur",
    "Blastoise", "Typhlosion", "Feraligatr", "Tyranitar", "Scizor", "Heracross", "Espeon", "Ampharos",
    "Steelix", "Ho-Oh", "Suicune", "Entei", "Raikou", "Celebi", "Blaziken", "Gardevoir", "Absol",
    "Metagross", "Flygon", "Milotic", "Lucario", "Garchomp", "Togekiss",
]
PROMO_SEARCHES = {
    "Black Star Promo": '("black star promo" OR "black star promos" OR "SVP promo" OR "SWSH promo")',
    "Pokemon Center Promo": '("pokemon center promo" OR "pokemon center exclusive" OR "pokecenter promo")',
    "Prerelease Promo": '("prerelease promo" OR "pre-release promo" OR "prerelease stamp")',
    "McDonald's Promo": '("mcdonald\'s pokemon" OR "mcdonalds pokemon" OR "mcdonald\'s promo")',
    "PSA 10 Pokemon": '("PSA 10" pokemon (card OR slab))',
}
SEED_CARDS = 27
# Best first. Seeding ranks by rarity because brand-new sets often have no prices yet.
RARITY_RANK = [
    "Mega Hyper Rare", "Special Illustration Rare", "Hyper Rare", "Ultra Rare",
    "Illustration Rare", "Double Rare",
]
CANDIDATE_RARITIES = set(RARITY_RANK)


def watchlist_path() -> str:
    return os.environ.get("HYPE_WATCHLIST_PATH", "watchlist.json")


def load_watchlist() -> dict | None:
    try:
        with open(watchlist_path()) as f:
            return json.load(f)
    except FileNotFoundError:
        return None


def save_watchlist(wl: dict) -> None:
    with open(watchlist_path(), "w") as f:
        json.dump(wl, f, indent=2, ensure_ascii=False)
        f.write("\n")


def _today_utc() -> date:
    return datetime.now(timezone.utc).date()


def _utc_midnight(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp())


def seed_watchlist() -> dict:
    """Build a watchlist from the top-rarity Pokémon of the newest sets, plus evergreen chase cards."""
    sets = tools.tcg_get("sets", {"orderBy": "-releaseDate", "pageSize": 20})["data"]
    # Skip promo and small sub-collections; they rarely drive hype on their own.
    sets = [s for s in sets if "promo" not in s["name"].lower() and s.get("total", 0) >= 60][:SEED_SETS]
    by_name: dict[str, dict] = {}
    for s in sets:
        try:
            cards = tools.tcg_get(
                "cards", {"q": f"set.id:{s['id']} supertype:Pokémon", "pageSize": 250, "select": "id,name,rarity"}
            )["data"]
        except Exception as e:  # The API is flaky; one missing set shouldn't stop seeding.
            print(f"  ! could not load set {s['name']}: {e}")
            continue
        for c in cards:
            if c.get("rarity") not in CANDIDATE_RARITIES:
                continue
            entry = by_name.setdefault(c["name"], {"name": c["name"], "card_ids": [], "rank": 99, "set": s["name"]})
            entry["card_ids"].append(c["id"])
            entry["rank"] = min(entry["rank"], RARITY_RANK.index(c["rarity"]))
    ranked = sorted(by_name.values(), key=lambda e: e["rank"])
    today = _today_utc().isoformat()
    cards = [{"name": n, "source": "evergreen", "added": today} for n in EVERGREEN]
    taken = {n.lower() for n in EVERGREEN}
    candidates = []
    for e in ranked:
        if e["name"].lower() in taken:
            continue
        item = {"name": e["name"], "card_ids": e["card_ids"], "set": e["set"]}
        if len(cards) < len(EVERGREEN) + SEED_CARDS and e["rank"] <= RARITY_RANK.index("Special Illustration Rare"):
            cards.append({**item, "source": "seed", "added": today})
        elif e["name"].lower().endswith(" ex"):
            # Plain names like "Lapras" would match every Lapras card ever printed.
            candidates.append(item)
        taken.add(e["name"].lower())
    cards += extra_entries()
    return {
        "about": "Cards the daily job tracks. Add {\"name\": \"...\"} entries to cards to track more "
        "(optional: \"query\" for a custom search, \"kind\": species/promo/card, \"cap\" tweets per day); "
        "remove entries to stop. candidates are names the weekly discovery search looks for.",
        "seeded_from_sets": [s["name"] for s in sets],
        "cards": cards,
        "candidates": candidates,
    }


def extra_entries() -> list[dict]:
    today = _today_utc().isoformat()
    out = [{"name": n, "kind": "species", "source": "species", "added": today, "cap": EXTRA_CAP,
            "query": f'"{n}" {CARD_WORDS}'} for n in SPECIES]
    out += [{"name": n, "kind": "promo", "source": "promo", "added": today, "cap": EXTRA_CAP, "query": q}
            for n, q in PROMO_SEARCHES.items()]
    return out


def add_extras(wl: dict) -> int:
    """Add any missing species/promo entries to an existing watchlist. Returns how many were added."""
    have = {c["name"].lower() for c in wl["cards"]}
    new = [e for e in extra_entries() if e["name"].lower() not in have]
    wl["cards"] += new
    return len(new)


def _name_pattern(name: str) -> re.Pattern:
    return re.compile(r"(?<!\w)" + re.escape(name.lower()).replace(r"\ ", r"[\s\-]*") + r"(?!\w)", re.IGNORECASE)


def discover(conn, key: str, wl: dict, budget: int) -> tuple[list[str], int]:
    """Broad weekly search: promote candidates that several different people tweeted about."""
    since = _utc_midnight(_today_utc() - timedelta(days=7))
    query = f'("pokemon card" OR "pokemon tcg" OR #PokemonTCG OR pulled) -giveaway -filter:retweets since_time:{since}'
    tweets, _ = tools.fetch_x(conn, key, query, min(DISCOVERY_TWEETS, budget), "Top")
    authors: dict[str, set] = {}
    patterns = {c["name"]: _name_pattern(c["name"]) for c in wl.get("candidates", [])}
    for t in tweets:
        text = (t.get("text") or "").lower()
        who = (t.get("author") or {}).get("userName")
        for name, pat in patterns.items():
            if pat.search(text):
                authors.setdefault(name, set()).add(who)
    promoted = sorted((n for n, a in authors.items() if len(a) >= DISCOVERY_MIN_AUTHORS), key=lambda n: -len(authors[n]))
    today = _today_utc().isoformat()
    for name in promoted:
        cand = next(c for c in wl["candidates"] if c["name"] == name)
        wl["candidates"].remove(cand)
        wl["cards"].append({**cand, "source": "discovery", "added": today, "discovery_authors": len(authors[name])})
    return promoted, len(tweets)


def estimate_day(tweets: list[dict], capped: bool, day_end: datetime) -> dict:
    """Turn one day's sample into estimated totals.

    If the sample hit the cap, the newest tweets were fetched first, so the time they span shows the
    posting rate at the end of the day; that rate is extrapolated over 24 hours. It's an estimate that
    ranks busy cards sensibly, not an exact count.
    """
    n = len(tweets)
    authors = Counter((t.get("author") or {}).get("userName") for t in tweets)
    est = float(n)
    if capped and n:
        times = [tools._parse_x_date(t.get("createdAt")) for t in tweets]
        times = [x for x in times if x]
        if times:
            covered = max((day_end - min(times)).total_seconds(), 600.0)
            est = max(n, n * 86400.0 / covered)
    return {
        "sampled": n,
        "capped": capped,
        "est_posts": round(est, 1),
        "est_unique_authors": round(est * len(authors) / n, 1) if n else 0.0,
        "engagement": sum(tools.tweet_engagement(t) for t in tweets),
        "top_author_posts": max(authors.values()) if authors else 0,
    }


def collect_x(conn, key: str, wl: dict, backfill_days: int, per_day_cap: int, budget: int) -> dict:
    today = _today_utc()
    days = [today - timedelta(days=i) for i in range(1, backfill_days + 1)]  # newest first; today isn't over yet
    done = {c["name"]: storage.estimated_days(conn, PLATFORM, c["name"]) for c in wl["cards"]}
    stats = {"days_collected": 0, "tweets": 0, "skipped_for_budget": 0}
    for day in days:
        for card in wl["cards"]:
            if day.isoformat() in done[card["name"]]:
                continue
            cap = card.get("cap", per_day_cap)
            if budget < cap:
                stats["skipped_for_budget"] += 1
                continue
            nxt = day + timedelta(days=1)
            # Unix-time bounds: TwitterAPI.io doesn't treat since:/until: dates as UTC days.
            search = card.get("query") or f'"{card["name"]}"'
            query = (f'{search} -giveaway -filter:retweets '
                     f"since_time:{_utc_midnight(day)} until_time:{_utc_midnight(nxt)}")
            used_before = storage.usage_today(conn, tools.X_PROVIDER)
            tweets, more = tools.fetch_x(conn, key, query, cap, "Latest")
            used = storage.usage_today(conn, tools.X_PROVIDER) - used_before
            budget -= used
            stats["tweets"] += used
            # Drop anything outside the day, just in case.
            tweets = [t for t in tweets if tools.tweet_record(t)["created_date"] == day.isoformat()]
            storage.record_posts(conn, PLATFORM, card["name"], [tools.tweet_record(t) for t in tweets])
            day_end = datetime(nxt.year, nxt.month, nxt.day, tzinfo=timezone.utc)
            storage.save_estimate(conn, day.isoformat(), PLATFORM, card["name"], **estimate_day(tweets, more, day_end))
            conn.commit()
            stats["days_collected"] += 1
    return stats


def collect_prices(conn, wl: dict) -> int:
    """Snapshot today's TCGplayer prices for every printing of each watchlist card name. Best effort.

    Returns how many printings had a price; brand-new sets often have none yet.
    """
    saved = 0
    deadline = time.monotonic() + PRICE_TIME_LIMIT_S
    for c in wl["cards"]:
        if c.get("kind", "card") != "card":
            continue
        if time.monotonic() > deadline:
            print(f"  ! price time limit reached; skipped the rest from {c['name']} on")
            break
        try:
            body = tools.tcg_get("cards", {"q": f'name:"{c["name"]}"', "pageSize": 50,
                                           "select": "id,name,set,tcgplayer"}, attempts=2, timeout=20.0)
        except Exception as e:
            print(f"  ! price lookup failed for {c['name']}: {e}")
            continue
        for card in body.get("data", []):
            if not (card.get("tcgplayer") or {}).get("prices"):
                continue
            storage.record_prices(
                conn,
                {"id": card["id"], "name": card["name"], "set": (card.get("set") or {}).get("name"),
                 "prices": (card.get("tcgplayer") or {}).get("prices", {})},
            )
            saved += 1
    return saved


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Collect daily X volume for the watchlist.")
    parser.add_argument("--backfill-days", type=int, default=7, help="How many past full days to fill in (default 7).")
    parser.add_argument("--per-day-cap", type=int, default=DEFAULT_PER_DAY_CAP, help="Max tweets per card per day.")
    parser.add_argument("--reseed", action="store_true", help="Rebuild the watchlist from the newest sets.")
    parser.add_argument("--discover", action="store_true", help="Run the discovery search even if not due.")
    args = parser.parse_args(argv)

    key = os.environ.get("TWITTERAPI_IO_KEY")
    if not key:
        raise SystemExit("TWITTERAPI_IO_KEY is not set.")

    wl = load_watchlist()
    if wl is None or args.reseed:
        print("Seeding watchlist from the Pokémon TCG API...")
        old = wl
        wl = seed_watchlist()
        if old:  # Keep cards someone added by hand or discovery found.
            names = {c["name"].lower() for c in wl["cards"]}
            wl["cards"] += [c for c in old["cards"] if c.get("source") not in ("seed", "evergreen")
                            and c["name"].lower() not in names]
        save_watchlist(wl)
    added = add_extras(wl)
    if added:
        save_watchlist(wl)
        print(f"Added {added} species/promo searches to the watchlist")
    print(f"Watchlist: {len(wl['cards'])} cards, {len(wl.get('candidates', []))} discovery candidates")

    with storage.connect() as conn:
        budget = max(0, tools._x_daily_cap() - storage.usage_today(conn, tools.X_PROVIDER))
        print(f"X budget for this run: {budget} tweets")

        last = storage.get_meta(conn, "last_discovery")
        due = not last or (_today_utc() - date.fromisoformat(last)).days >= DISCOVERY_EVERY_DAYS
        if (due or args.discover) and wl.get("candidates") and budget >= DISCOVERY_TWEETS:
            promoted, used = discover(conn, key, wl, budget)
            budget -= used
            storage.set_meta(conn, "last_discovery", _today_utc().isoformat())
            save_watchlist(wl)
            print(f"Discovery: read {used} tweets, added {promoted or 'nothing'}")

        stats = collect_x(conn, key, wl, args.backfill_days, args.per_day_cap, budget)
        print(f"X: collected {stats['days_collected']} card-days from {stats['tweets']} tweets; "
              f"{stats['skipped_for_budget']} card-days left for a later run")
        print(f"Prices: saved {collect_prices(conn, wl)} priced printings")
        print(f"X tweets used today: {storage.usage_today(conn, tools.X_PROVIDER)}")

    board = json.loads(tools.hype_leaderboard.call({"period": "day"}))
    if "cards" in board:
        print(f"Top cards on {board['window']['end']} (UTC), by unique authors:")
        for i, c in enumerate(board["cards"], 1):
            print(f"  {i}. {c['subject']}: ~{c['est_unique_authors']:.0f} people, ~{c['est_posts']:.0f} posts")


if __name__ == "__main__":
    main()

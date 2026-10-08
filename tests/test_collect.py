"""Collection job and leaderboard tests, against fake API responses."""

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from hype_agent import collect, storage, tools

TODAY = datetime.now(timezone.utc).date()


def at(day, hour, minute=0) -> str:
    d = datetime(day.year, day.month, day.day, hour, minute, tzinfo=timezone.utc)
    return d.strftime("%a %b %d %H:%M:%S %z %Y")


def tweet(id_, user, created, likes=1):
    return {"id": id_, "text": "t", "likeCount": likes, "createdAt": created,
            "author": {"userName": user, "followers": 10, "createdAt": at(TODAY - timedelta(days=900), 0)}}


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HYPE_DB_PATH", str(tmp_path / "hype.db"))
    monkeypatch.setenv("HYPE_WATCHLIST_PATH", str(tmp_path / "watchlist.json"))
    monkeypatch.setenv("TWITTERAPI_IO_KEY", "test-key")
    monkeypatch.delenv("X_DAILY_TWEET_CAP", raising=False)


def test_estimate_extrapolates_only_when_capped():
    day = TODAY - timedelta(days=1)
    end = datetime(TODAY.year, TODAY.month, TODAY.day, tzinfo=timezone.utc)
    # 40 tweets by 20 authors covering the last 6 hours of the day.
    tweets = [tweet(str(i), f"u{i % 20}", at(day, 18 + i % 6)) for i in range(40)]
    est = collect.estimate_day(tweets, capped=True, day_end=end)
    assert est["est_posts"] == pytest.approx(160)  # 40 tweets in 6h -> 160 per 24h
    assert est["est_unique_authors"] == pytest.approx(80)
    assert est["top_author_posts"] == 2
    assert collect.estimate_day(tweets, capped=False, day_end=end)["est_posts"] == 40


def test_name_pattern_matches_variants_not_substrings():
    pat = collect._name_pattern("Umbreon ex")
    assert pat.search("pulled umbreon ex psa 10!")
    assert pat.search("#Umbreon-ex")
    assert not pat.search("umbreon exclusive")


def search_router(by_day: dict, calls: list):
    """Fake TwitterAPI.io: returns the tweets for the 'since:' day in the query, one page."""

    def _get(url, params=None, headers=None, **kwargs):
        calls.append(params["query"])
        start = int(params["query"].split("since_time:")[1].split()[0])
        day = datetime.fromtimestamp(start, timezone.utc).date().isoformat()
        tweets = by_day.get((params["query"].split('"')[1], day), [])
        return httpx.Response(200, json={"tweets": tweets, "has_next_page": False}, request=httpx.Request("GET", url))

    return _get


def test_collect_fills_missing_days_once_and_feeds_leaderboard(monkeypatch):
    y1, y2 = TODAY - timedelta(days=1), TODAY - timedelta(days=2)
    by_day = {
        ("Umbreon ex", y1.isoformat()): [tweet(f"a{i}", f"fan{i}", at(y1, 12)) for i in range(6)],
        ("Pikachu ex", y1.isoformat()): [tweet("p1", "solo", at(y1, 9)), tweet("p2", "solo", at(y1, 10))],
        ("Pikachu ex", y2.isoformat()): [tweet("p3", "x", at(y2, 9))],
    }
    calls = []
    monkeypatch.setattr(tools.httpx, "get", search_router(by_day, calls))
    wl = {"cards": [{"name": "Umbreon ex"}, {"name": "Pikachu ex"}], "candidates": []}

    with storage.connect() as conn:
        stats = collect.collect_x(conn, "k", wl, backfill_days=2, per_day_cap=40, budget=1000)
    assert stats["days_collected"] == 4
    assert all("-giveaway -filter:retweets" in q for q in calls)
    assert f"since_time:{collect._utc_midnight(y1)} until_time:{collect._utc_midnight(TODAY)}" in calls[0]

    calls.clear()
    with storage.connect() as conn:  # Second run: nothing left to fetch.
        assert collect.collect_x(conn, "k", wl, 2, 40, 1000)["days_collected"] == 0
    assert calls == []

    out = json.loads(tools.hype_leaderboard.call({"period": "day"}))
    assert out["window"]["end"] == y1.isoformat()
    assert [c["subject"] for c in out["cards"]] == ["umbreon ex", "pikachu ex"]
    assert out["cards"][1]["top_author_share_of_sample"] == 1.0

    week = json.loads(tools.hype_leaderboard.call({"period": "week"}))
    assert {c["subject"]: c["est_posts"] for c in week["cards"]} == {"umbreon ex": 6, "pikachu ex": 3}


def test_collect_stops_at_budget_and_leaves_days_for_later(monkeypatch):
    monkeypatch.setattr(tools.httpx, "get", search_router({}, []))
    wl = {"cards": [{"name": "A"}, {"name": "B"}], "candidates": []}
    with storage.connect() as conn:
        stats = collect.collect_x(conn, "k", wl, backfill_days=3, per_day_cap=40, budget=40)
    # Empty results cost nothing, so all fit; with a budget below the cap nothing runs.
    assert stats["days_collected"] == 6
    with storage.connect() as conn:
        conn.execute("DELETE FROM social_estimates")
        stats = collect.collect_x(conn, "k", wl, backfill_days=3, per_day_cap=40, budget=39)
    assert stats == {"days_collected": 0, "tweets": 0, "skipped_for_budget": 6}


def test_rising_compares_with_previous_days():
    with storage.connect() as conn:
        for i in range(1, 9):
            day = (TODAY - timedelta(days=i)).isoformat()
            hot = 50 if i == 1 else 5
            for subject, posts in (("hot", hot), ("steady", 20)):
                storage.save_estimate(conn, day, "x", subject, sampled=posts, capped=False, est_posts=posts,
                                      est_unique_authors=posts, engagement=0, top_author_posts=1)
    out = json.loads(tools.hype_leaderboard.call({"period": "day", "sort": "rising"}))
    assert out["cards"][0]["subject"] == "hot"
    assert out["cards"][0]["change_pct"] == 900


def test_leaderboard_without_data():
    assert "not run" in json.loads(tools.hype_leaderboard.call({}))["error"]


def test_discovery_promotes_candidates_with_several_authors(monkeypatch):
    tweets = [tweet(str(i), f"u{i}", at(TODAY, 1), likes=0) | {"text": "Look at my Mega Gengar EX pull"}
              for i in range(3)]
    tweets.append(tweet("9", "u9", at(TODAY, 1)) | {"text": "Lillie's Clefairy ex"})
    page = {"tweets": tweets, "has_next_page": False}
    monkeypatch.setattr(tools.httpx, "get",
                        lambda url, **kw: httpx.Response(200, json=page, request=httpx.Request("GET", url)))
    wl = {"cards": [], "candidates": [{"name": "Mega Gengar ex", "card_ids": ["me5-1"]},
                                      {"name": "Lillie's Clefairy ex", "card_ids": []}]}
    with storage.connect() as conn:
        promoted, used = collect.discover(conn, "k", wl, budget=500)
    assert promoted == ["Mega Gengar ex"] and used == 4
    assert wl["cards"][0]["source"] == "discovery"
    assert [c["name"] for c in wl["candidates"]] == ["Lillie's Clefairy ex"]


def test_quoted_and_plain_subjects_share_history(monkeypatch):
    page = {"tweets": [tweet("1", "a", at(TODAY, 0))], "has_next_page": False}
    monkeypatch.setattr(tools.httpx, "get",
                        lambda url, **kw: httpx.Response(200, json=page, request=httpx.Request("GET", url)))
    tools.x_buzz.call({"query": '"Umbreon ex"'})
    out = json.loads(tools.hype_history.call({"subject": "Umbreon ex"}))
    assert out["social"][0]["posts"] == 1


def test_species_and_promo_entries_use_custom_queries_and_caps(monkeypatch):
    y1 = TODAY - timedelta(days=1)
    wl = {"cards": [{"name": "Umbreon ex"}], "candidates": []}
    assert collect.add_extras(wl) == len(collect.SPECIES) + len(collect.PROMO_SEARCHES)
    assert collect.add_extras(wl) == 0  # Idempotent.

    calls = []

    def fake_get(url, params=None, **kw):
        calls.append(params["query"])
        return httpx.Response(200, json={"tweets": [tweet("1", "u", at(y1, 12))], "has_next_page": False},
                              request=httpx.Request("GET", url))

    monkeypatch.setattr(tools.httpx, "get", fake_get)
    wl["cards"] = [wl["cards"][0]] + [c for c in wl["cards"] if c["name"] in ("Aerodactyl", "Black Star Promo")]
    with storage.connect() as conn:
        collect.collect_x(conn, "k", wl, backfill_days=1, per_day_cap=40, budget=1000)
    assert any(q.startswith('"Umbreon ex" -giveaway') for q in calls)
    assert any(q.startswith('"Aerodactyl" ("pokemon card"') for q in calls)
    assert any(q.startswith('("black star promo"') for q in calls)


def test_leaderboard_kind_filter(monkeypatch):
    y1 = TODAY - timedelta(days=1)
    wl = {"cards": [{"name": "Umbreon ex"}, {"name": "Aerodactyl", "kind": "species"}], "candidates": []}
    collect.save_watchlist(wl)
    with storage.connect() as conn:
        for name in ("Umbreon ex", "Aerodactyl"):
            storage.save_estimate(conn, y1.isoformat(), "x", name, sampled=3, capped=False, est_posts=3.0,
                                  est_unique_authors=3.0, engagement=1, top_author_posts=1)
    names = lambda kind: [c["subject"] for c in json.loads(tools.hype_leaderboard.call({"kind": kind}))["cards"]]
    assert names("card") == ["umbreon ex"]
    assert names("species") == ["aerodactyl"]
    assert set(names("all")) == {"umbreon ex", "aerodactyl"}

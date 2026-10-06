"""Tests run against fake API responses, so they never spend API credits."""

import json
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest

from hype_agent import storage, tools

TODAY = date.today()


def x_date(days_ago: int = 0) -> str:
    d = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return d.strftime("%a %b %d %H:%M:%S %z %Y")


def tweet(id_, user, likes=1, followers=100, days_ago=0, account_days=1000):
    return {
        "id": id_,
        "url": f"https://x.com/{user}/status/{id_}",
        "text": f"tweet {id_}",
        "likeCount": likes,
        "retweetCount": 0,
        "replyCount": 0,
        "quoteCount": 0,
        "viewCount": 10,
        "createdAt": x_date(days_ago),
        "author": {"userName": user, "followers": followers, "createdAt": x_date(account_days)},
    }


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setenv("HYPE_DB_PATH", str(tmp_path / "hype.db"))
    monkeypatch.setenv("TWITTERAPI_IO_KEY", "test-key")
    monkeypatch.delenv("X_DAILY_TWEET_CAP", raising=False)


def fake_get(pages: list[dict], calls: list):
    def _get(url, params=None, headers=None, **kwargs):
        calls.append({"url": url, "params": params, "headers": headers})
        body = pages[min(len(calls) - 1, len(pages) - 1)]
        return httpx.Response(200, json=body, request=httpx.Request("GET", url))

    return _get


def test_x_buzz_without_key(monkeypatch):
    monkeypatch.delenv("TWITTERAPI_IO_KEY")
    out = json.loads(tools.x_buzz.call({"query": "Umbreon ex"}))
    assert "not configured" in out["error"]


def test_x_buzz_summarizes_and_saves(monkeypatch):
    calls = []
    page = {
        "tweets": [
            tweet("1", "alice", likes=50, followers=5000),
            tweet("2", "bob", likes=5, account_days=10),
            tweet("3", "alice", likes=1, days_ago=1),
        ],
        "has_next_page": False,
    }
    monkeypatch.setattr(tools.httpx, "get", fake_get([page], calls))

    out = json.loads(tools.x_buzz.call({"query": "Umbreon ex", "max_tweets": 20}))

    assert calls[0]["headers"]["X-API-Key"] == "test-key"
    assert calls[0]["params"]["query"] == "Umbreon ex -filter:retweets"
    assert out["tweets"] == 3
    assert out["unique_authors"] == 2
    assert out["likes"] == 56
    assert out["authors_with_accounts_under_90_days"] == 1
    assert out["top_tweets"][0]["author"] == "alice"
    assert out["tweets_used_today"] == 3
    by_date = {row["date"]: row for row in out["daily_history"]}
    assert by_date[TODAY.isoformat()]["posts"] == 2
    assert by_date[(TODAY - timedelta(days=1)).isoformat()]["posts"] == 1


def test_x_buzz_does_not_double_count_on_rerun(monkeypatch):
    page = {"tweets": [tweet("1", "alice"), tweet("2", "bob")], "has_next_page": False}
    monkeypatch.setattr(tools.httpx, "get", fake_get([page], []))
    tools.x_buzz.call({"query": "Umbreon ex"})
    out = json.loads(tools.x_buzz.call({"query": "umbreon  EX"}))  # same subject, different spacing/case
    assert out["daily_history"] == [
        {"date": TODAY.isoformat(), "platform": "x", "posts": 2, "unique_authors": 2, "engagement": 2}
    ]
    assert out["tweets_used_today"] == 4  # both calls were billed


def test_x_buzz_stops_when_pagination_repeats(monkeypatch):
    calls = []
    page = {"tweets": [tweet(str(i), f"u{i}") for i in range(20)], "has_next_page": True, "next_cursor": "c1"}
    monkeypatch.setattr(tools.httpx, "get", fake_get([page], calls))
    out = json.loads(tools.x_buzz.call({"query": "Charizard", "max_tweets": 100}))
    assert len(calls) == 2  # second page was identical, so it stopped
    assert out["tweets"] == 20


def test_x_buzz_follows_cursor_until_max(monkeypatch):
    calls = []
    pages = [
        {"tweets": [tweet(str(i), f"u{i}") for i in range(20)], "has_next_page": True, "next_cursor": "c1"},
        {"tweets": [tweet(str(i), f"u{i}") for i in range(20, 40)], "has_next_page": True, "next_cursor": "c2"},
    ]
    monkeypatch.setattr(tools.httpx, "get", fake_get(pages, calls))
    out = json.loads(tools.x_buzz.call({"query": "Charizard", "max_tweets": 30}))
    assert [c["params"]["cursor"] for c in calls] == ["", "c1"]
    assert out["tweets"] == 30


def test_x_buzz_respects_daily_cap(monkeypatch):
    monkeypatch.setenv("X_DAILY_TWEET_CAP", "20")
    calls = []
    page = {"tweets": [tweet(str(i), f"u{i}") for i in range(20)], "has_next_page": True, "next_cursor": "c1"}
    monkeypatch.setattr(tools.httpx, "get", fake_get([page], calls))
    tools.x_buzz.call({"query": "Charizard", "max_tweets": 100})
    assert len(calls) == 1
    out = json.loads(tools.x_buzz.call({"query": "Charizard"}))
    assert "cap" in out["error"]
    assert len(calls) == 1


def test_search_cards_saves_price_snapshot(monkeypatch):
    body = {
        "data": [
            {
                "id": "sv8pt5-161",
                "name": "Umbreon ex",
                "set": {"name": "Prismatic Evolutions", "releaseDate": "2025/01/17"},
                "rarity": "Special Illustration Rare",
                "tcgplayer": {"prices": {"holofoil": {"market": 1200.5, "low": 1100, "high": 1500}}},
            }
        ]
    }
    monkeypatch.setattr(tools.httpx, "get", fake_get([body], []))
    tools.search_cards.call({"query": "name:umbreon"})
    out = json.loads(tools.hype_history.call({"subject": "Umbreon ex"}))
    assert out["prices"][0]["market"] == 1200.5
    assert out["prices"][0]["variant"] == "holofoil"


def test_reddit_buzz_saves_daily_counts(monkeypatch):
    now = datetime.now(timezone.utc).timestamp()
    body = {
        "data": {
            "children": [
                {"data": {"id": "a1", "author": "x", "title": "t", "score": 10, "num_comments": 2,
                          "created_utc": now, "permalink": "/r/p/a1"}},
                {"data": {"id": "a2", "author": "y", "title": "t", "score": 3, "num_comments": 0,
                          "created_utc": now, "permalink": "/r/p/a2"}},
            ]
        }
    }
    monkeypatch.setattr(tools.httpx, "get", fake_get([body], []))
    tools.reddit_buzz.call({"query": "Umbreon ex"})
    out = json.loads(tools.hype_history.call({"subject": "Umbreon ex"}))
    assert out["social"] == [
        {"date": TODAY.isoformat(), "platform": "reddit/pokemontcg", "posts": 2, "unique_authors": 2, "engagement": 15}
    ]


def test_old_posts_are_pruned_but_daily_totals_kept():
    old_day = (TODAY - timedelta(days=storage.POST_RETENTION_DAYS - 1)).isoformat()
    with storage.connect() as conn:
        storage.record_posts(conn, "x", "pikachu", [{"id": "1", "author": "a", "created_date": old_day}])
    with storage.connect() as conn:
        conn.execute("UPDATE posts SET created_date = '2000-01-01'")
        storage.record_posts(conn, "x", "pikachu", [])
        assert conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 0
        assert storage.social_history(conn, "pikachu", days=60)[0]["posts"] == 1

"""SQLite history store: daily price snapshots and social buzz, so runs build a trend over time."""

import os
import sqlite3
from contextlib import contextmanager
from datetime import date, timedelta

# Raw posts are kept this long (for dedupe and daily rollups), then pruned.
# Daily totals in social_daily are kept forever.
POST_RETENTION_DAYS = 30

SCHEMA = """
CREATE TABLE IF NOT EXISTS price_snapshots (
    date TEXT NOT NULL,
    card_id TEXT NOT NULL,
    card_name TEXT,
    set_name TEXT,
    source TEXT NOT NULL,
    variant TEXT NOT NULL,
    market REAL,
    low REAL,
    high REAL,
    PRIMARY KEY (date, card_id, source, variant)
);
CREATE TABLE IF NOT EXISTS posts (
    platform TEXT NOT NULL,
    post_id TEXT NOT NULL,
    subject TEXT NOT NULL,
    author TEXT,
    author_followers INTEGER,
    engagement INTEGER,
    created_date TEXT NOT NULL,
    PRIMARY KEY (platform, post_id, subject)
);
CREATE TABLE IF NOT EXISTS social_daily (
    date TEXT NOT NULL,
    platform TEXT NOT NULL,
    subject TEXT NOT NULL,
    posts INTEGER NOT NULL,
    unique_authors INTEGER NOT NULL,
    engagement INTEGER NOT NULL,
    PRIMARY KEY (date, platform, subject)
);
CREATE TABLE IF NOT EXISTS social_estimates (
    date TEXT NOT NULL,
    platform TEXT NOT NULL,
    subject TEXT NOT NULL,
    sampled INTEGER NOT NULL,
    capped INTEGER NOT NULL,
    est_posts REAL NOT NULL,
    est_unique_authors REAL NOT NULL,
    engagement INTEGER NOT NULL,
    top_author_posts INTEGER NOT NULL,
    PRIMARY KEY (date, platform, subject)
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS api_usage (
    date TEXT NOT NULL,
    provider TEXT NOT NULL,
    units INTEGER NOT NULL,
    PRIMARY KEY (date, provider)
);
"""


def db_path() -> str:
    return os.environ.get("HYPE_DB_PATH", "hype.db")


@contextmanager
def connect():
    conn = sqlite3.connect(db_path())
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


def normalize_subject(subject: str) -> str:
    # Quotes are X/Reddit phrase syntax, not part of the subject: '"Umbreon ex"' == 'Umbreon ex'.
    return " ".join(subject.replace('"', " ").lower().split())


def _today() -> str:
    return date.today().isoformat()


def record_prices(conn: sqlite3.Connection, card: dict, source: str = "tcgplayer") -> None:
    """Save today's price for each variant of a card (e.g. holofoil, reverseHolofoil)."""
    for variant, p in (card.get("prices") or {}).items():
        conn.execute(
            "INSERT OR REPLACE INTO price_snapshots VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (_today(), card["id"], card.get("name"), card.get("set"), source, variant,
             p.get("market"), p.get("low"), p.get("high")),
        )


def record_posts(conn: sqlite3.Connection, platform: str, subject: str, posts: list[dict]) -> int:
    """Save posts not seen before and refresh the daily totals they fall in. Returns how many were new.

    Each post needs: id, author, author_followers, engagement, created_date (YYYY-MM-DD).
    Posts older than the retention window are skipped, since their day is already final.
    """
    subject = normalize_subject(subject)
    cutoff = (date.today() - timedelta(days=POST_RETENTION_DAYS)).isoformat()
    new, touched = 0, set()
    for p in posts:
        if p["created_date"] < cutoff:
            continue
        cur = conn.execute(
            "INSERT OR IGNORE INTO posts VALUES (?, ?, ?, ?, ?, ?, ?)",
            (platform, str(p["id"]), subject, p.get("author"), p.get("author_followers"),
             p.get("engagement", 0), p["created_date"]),
        )
        if cur.rowcount:
            new += 1
            touched.add(p["created_date"])
    for day in touched:
        conn.execute(
            """INSERT OR REPLACE INTO social_daily
               SELECT created_date, platform, subject, COUNT(*), COUNT(DISTINCT author),
                      COALESCE(SUM(engagement), 0)
               FROM posts WHERE platform = ? AND subject = ? AND created_date = ?
               GROUP BY created_date, platform, subject""",
            (platform, subject, day),
        )
    conn.execute("DELETE FROM posts WHERE created_date < ?", (cutoff,))
    return new


def social_history(conn: sqlite3.Connection, subject: str, days: int = 14) -> list[dict]:
    since = (date.today() - timedelta(days=days)).isoformat()
    rows = conn.execute(
        """SELECT date, platform, posts, unique_authors, engagement FROM social_daily
           WHERE subject = ? AND date >= ? ORDER BY date, platform""",
        (normalize_subject(subject), since),
    )
    return [dict(r) for r in rows]


def price_history(conn: sqlite3.Connection, card_name: str, days: int = 30) -> list[dict]:
    since = (date.today() - timedelta(days=days)).isoformat()
    rows = conn.execute(
        """SELECT date, card_id, card_name, set_name, source, variant, market FROM price_snapshots
           WHERE card_name LIKE ? AND date >= ? ORDER BY card_id, variant, date""",
        (f"%{card_name}%", since),
    )
    return [dict(r) for r in rows]


def usage_today(conn: sqlite3.Connection, provider: str) -> int:
    row = conn.execute(
        "SELECT units FROM api_usage WHERE date = ? AND provider = ?", (_today(), provider)
    ).fetchone()
    return row["units"] if row else 0


def add_usage(conn: sqlite3.Connection, provider: str, units: int) -> None:
    conn.execute(
        """INSERT INTO api_usage VALUES (?, ?, ?)
           ON CONFLICT (date, provider) DO UPDATE SET units = units + excluded.units""",
        (_today(), provider, units),
    )


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, value))


def save_estimate(conn: sqlite3.Connection, day: str, platform: str, subject: str, **fields) -> None:
    """Save one full day's measured volume for a watchlist subject (see collect.py for how it's estimated)."""
    conn.execute(
        "INSERT OR REPLACE INTO social_estimates VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (day, platform, normalize_subject(subject), fields["sampled"], int(fields["capped"]),
         fields["est_posts"], fields["est_unique_authors"], fields["engagement"], fields["top_author_posts"]),
    )


def estimated_days(conn: sqlite3.Connection, platform: str, subject: str) -> set[str]:
    rows = conn.execute(
        "SELECT date FROM social_estimates WHERE platform = ? AND subject = ?",
        (platform, normalize_subject(subject)),
    )
    return {r["date"] for r in rows}


def latest_estimate_date(conn: sqlite3.Connection, platform: str) -> str | None:
    row = conn.execute("SELECT MAX(date) AS d FROM social_estimates WHERE platform = ?", (platform,)).fetchone()
    return row["d"]


def estimate_totals(conn: sqlite3.Connection, platform: str, start: str, end: str) -> dict[str, dict]:
    """Per-subject totals of daily estimates for dates in [start, end]."""
    rows = conn.execute(
        """SELECT subject, COUNT(*) AS days, SUM(sampled) AS sampled, SUM(capped) AS capped_days,
                  SUM(est_posts) AS est_posts, SUM(est_unique_authors) AS est_unique_authors,
                  SUM(engagement) AS engagement, MAX(top_author_posts) AS top_author_posts
           FROM social_estimates WHERE platform = ? AND date BETWEEN ? AND ? GROUP BY subject""",
        (platform, start, end),
    )
    return {r["subject"]: dict(r) for r in rows}


def latest_prices(conn: sqlite3.Connection, card_name: str, limit: int = 3) -> list[dict]:
    """Latest market prices of the priciest printings with this exact card name (one row per printing)."""
    rows = conn.execute(
        """SELECT p.date, p.card_id, p.set_name, p.variant, MAX(p.market) AS market FROM price_snapshots p
           WHERE lower(p.card_name) = lower(?) AND p.market IS NOT NULL
             AND p.date = (SELECT MAX(date) FROM price_snapshots q WHERE q.card_id = p.card_id)
           GROUP BY p.card_id ORDER BY market DESC LIMIT ?""",
        (card_name, limit),
    )
    return [dict(r) for r in rows]

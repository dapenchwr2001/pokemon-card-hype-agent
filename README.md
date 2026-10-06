# Pokémon Card Hype Agent

An AI agent that tracks hype around Pokémon TCG cards. It pulls live prices
and recent community buzz, then writes a short hype report. It runs on Claude.

## How it works

The agent uses Claude's tool use. Claude decides which tools to call:

| Tool | Source | What it gives |
|---|---|---|
| `search_cards` | [Pokémon TCG API](https://pokemontcg.io) | Card details and TCGplayer market prices |
| `reddit_buzz` | Reddit search | Last week's posts, scores, and comment counts |
| `x_buzz` | X search via [TwitterAPI.io](https://twitterapi.io) | Recent tweets, engagement, unique authors, new-account share |
| `hype_history` | Local SQLite history | Daily mention counts and prices from earlier runs |

Claude then combines the results into a report with a 1-10 hype score per card.

### History

Every tool call saves a snapshot to a SQLite file (`hype.db`, or set `HYPE_DB_PATH`):
daily prices per card and variant, and daily post counts, unique authors, and
engagement per search term and platform. Re-running a search doesn't double-count
posts. Raw post IDs are kept for 30 days for de-duplication; daily totals are kept
forever. Run the agent on the same cards each day and `hype_history` shows the trend.

### X costs

TwitterAPI.io bills per tweet returned (about $0.15 per 1,000). `x_buzz` stops at a
daily cap of 1,500 tweets by default; change it with `X_DAILY_TWEET_CAP`.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .
export ANTHROPIC_API_KEY=sk-ant-...
# optional, for higher rate limits:
export POKEMONTCG_API_KEY=...
# optional, enables X search:
export TWITTERAPI_IO_KEY=...
```

On Windows PowerShell, activate with `.venv\Scripts\Activate.ps1` and set keys with
`$env:TWITTERAPI_IO_KEY = "..."`.

Run the tests (they use fake API responses and spend no credits):

```bash
pip install -e ".[dev]"
pytest
```

## Usage

```bash
hype "How hyped is Umbreon ex from Prismatic Evolutions?"
hype "Which Charizard cards are trending right now?"
```

## Roadmap ideas

- ~~Save price snapshots over time to track momentum~~ (done)
- ~~Add X mentions~~ (done)
- Add graded (PSA 9/10) prices and population counts
- Add more signals: eBay sold listings, YouTube mentions, Google Trends
- Momentum score: mention growth vs. price change
- Run on a schedule and post a daily "hype leaderboard"
- Add a web dashboard

> Hype scores are for fun and research. This is not financial advice.

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
| `hype_leaderboard` | Daily collection job | Top watchlist cards for the latest day or week, by volume or rising |

Claude then combines the results into a report with a 1-10 hype score per card.

### History

Every tool call saves a snapshot to a SQLite file (`hype.db`, or set `HYPE_DB_PATH`):
daily prices per card and variant, and daily post counts, unique authors, and
engagement per search term and platform. Re-running a search doesn't double-count
posts. Raw post IDs are kept for 30 days for de-duplication; daily totals are kept
forever. Run the agent on the same cards each day and `hype_history` shows the trend.

### Daily collection and leaderboard

Broad questions ("hottest cards today?", "top 5 this week?") can't be answered by searching card by
card, so a daily job measures every card on a watchlist the same way:

```bash
python -m hype_agent.collect          # or: hype-collect
```

- **Watchlist** (`watchlist.json`, or set `HYPE_WATCHLIST_PATH`): built on the first run from the
  top-rarity Pokémon of the 4 newest sets plus a few evergreen chase cards. Edit it freely: add
  `{"name": "Card Name ex"}` to `cards` to track a card, delete an entry to stop.
  `python -m hype_agent.collect --reseed` rebuilds it, keeping cards you added.
- **X volume per full UTC day**: up to 40 tweets per card per day (excluding retweets and
  "giveaway" tweets). If a day has more, the total is estimated from how fast the newest tweets
  arrived. Missing days (up to `--backfill-days`, default 7) are filled in newest first until the
  run's tweet budget is spent; the rest wait for the next run.
- **Discovery**: once a week, a broad Pokémon TCG search (200 tweets) adds candidate cards from the
  newest sets that 3 or more different people tweeted about.
- **Prices**: a daily TCGplayer price snapshot for every printing of each watchlist card.

`hype_leaderboard` ranks cards by estimated unique authors for the latest collected day or the last
7 days, or by growth vs. the previous period (`sort="rising"`).

#### Scheduled run

`scripts/daily_collect.sh` is what the scheduled run executes. History and the watchlist live on the
`hype-data` branch, not with the code; the script restores them, runs the collection, and pushes them
back as a single fresh commit (the database holds the history, so old copies aren't kept). To read the
latest data locally:

```bash
git fetch origin hype-data && git worktree add ../hype-data origin/hype-data
HYPE_DB_PATH=../hype-data/hype.db hype "What are the 5 hottest cards this week?"
```

### X costs

TwitterAPI.io bills per tweet returned (about $0.15 per 1,000). `x_buzz` stops at a
daily cap of 1,500 tweets by default; change it with `X_DAILY_TWEET_CAP`. The cap is shared by
`x_buzz` and the collection job. The first 25-card watchlist used about 1,600 tweets for its 7-day
backfill (roughly 250 a day, about $0.04); the hard ceiling is 40 per card per day.

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
- ~~Run on a schedule and collect a daily leaderboard~~ (done; posting it somewhere is next)
- Add a web dashboard

> Hype scores are for fun and research. This is not financial advice.

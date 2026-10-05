# Pokémon Card Hype Agent

An AI agent that tracks hype around Pokémon TCG cards. It pulls live prices
and recent community buzz, then writes a short hype report. It runs on Claude.

## How it works

The agent uses Claude's tool use. Claude decides which tools to call:

| Tool | Source | What it gives |
|---|---|---|
| `search_cards` | [Pokémon TCG API](https://pokemontcg.io) | Card details and TCGplayer market prices |
| `reddit_buzz` | Reddit search | Last week's posts, scores, and comment counts |

Claude then combines the results into a report with a 1-10 hype score per card.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .
export ANTHROPIC_API_KEY=sk-ant-...
# optional, for higher rate limits:
export POKEMONTCG_API_KEY=...
```

## Usage

```bash
hype "How hyped is Umbreon ex from Prismatic Evolutions?"
hype "Which Charizard cards are trending right now?"
```

## Roadmap ideas

- Save price snapshots over time to track momentum
- Add more signals: eBay sold listings, YouTube and X mentions, Google Trends
- Run on a schedule and post a daily "hype leaderboard"
- Add a web dashboard

> Hype scores are for fun and research. This is not financial advice.

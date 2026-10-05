"""Data-gathering tools the hype agent can call."""

import json
import os

import httpx
from anthropic import beta_tool

POKEMON_TCG_API = "https://api.pokemontcg.io/v2"
REDDIT_SEARCH = "https://www.reddit.com/r/{subreddit}/search.json"
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
    posts = [
        {
            "title": p["data"]["title"],
            "score": p["data"]["score"],
            "comments": p["data"]["num_comments"],
            "created_utc": p["data"]["created_utc"],
            "url": "https://reddit.com" + p["data"]["permalink"],
        }
        for p in resp.json().get("data", {}).get("children", [])
    ]
    return json.dumps(posts)


ALL_TOOLS = [search_cards, reddit_buzz]

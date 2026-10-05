"""Claude-powered agent that produces a hype report for Pokémon cards."""

import argparse

import anthropic

from hype_agent.tools import ALL_TOOLS

MODEL = "claude-opus-5-5"

SYSTEM_PROMPT = """You are a Pokémon TCG market analyst. Given a card, set, or \
question, use your tools to gather current TCGplayer prices and recent community \
discussion, then write a concise hype report.

For each card you cover, include:
- Current market price (and holo/reverse variants if relevant)
- Community buzz: post volume, engagement, and the overall sentiment
- A hype score from 1-10 with a one-line justification

Base every claim on tool results and say when data is thin. This is \
entertainment and research, not financial advice."""


def run(question: str) -> str:
    client = anthropic.Anthropic()
    runner = client.beta.messages.tool_runner(
        model=MODEL,
        max_tokens=16000,
        output_config={"effort": "medium"},
        # Re-run on a fallback model if a safety classifier declines the request.
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        system=SYSTEM_PROMPT,
        tools=ALL_TOOLS,
        messages=[{"role": "user", "content": question}],
    )

    final = None
    for message in runner:
        final = message
        for block in message.content:
            if block.type == "tool_use":
                print(f"  -> {block.name}({block.input})")

    if final is None:
        return ""
    if final.stop_reason == "refusal":
        return "The request was declined."
    return "".join(b.text for b in final.content if b.type == "text")


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate a Pokémon card hype report.")
    parser.add_argument("question", help="e.g. 'How hyped is Umbreon ex from Prismatic Evolutions?'")
    args = parser.parse_args()
    print(run(args.question))


if __name__ == "__main__":
    main()

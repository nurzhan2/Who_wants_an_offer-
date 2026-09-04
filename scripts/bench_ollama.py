"""Measure local inference before phase 3b relies on it.

    make bench-ollama

Telegram parsing is routed to a local model because the task is simple, the
volume is large and nothing waits on it. Whether that is actually a good idea
on a given machine is a question about tokens per second, and the honest answer
comes from a stopwatch rather than an opinion.

This script runs one realistic post through the configured model ten times,
reports the median, projects it to a nightly batch, and prices the same batch
through the API for comparison. Then you decide with two numbers in front of
you instead of one intuition.

The fork it exists to settle: if the local model manages fewer than about sixty
posts an hour, a nightly run stops being "free overnight" and starts being
three hours of a laptop fan. At that point either a smaller model — the task is
"post to JSON", not reasoning — or Haiku through the API is the better trade,
and the API figure is usually smaller than a month of lunches.
"""

import asyncio
import json
import statistics
import sys
import time

import httpx

from app.core.config import settings
from app.llm.pricing import MILLION

RUNS = 10
#: A realistic Telegram job post: short, messy, Russian, with the details that
#: actually have to survive extraction.
SAMPLE_POST = """
🔥 Backend-разработчик (Python) — Алматы / гибрид

Компания: Астана-Финтех
Вилка: 800 000 – 1 200 000 ₸ на руки
Опыт: от 3 лет

Что нужно: Python 3.11+, FastAPI или Django, PostgreSQL, Docker.
Плюсом: Kafka, Kubernetes, опыт с высоконагруженными сервисами.
Оформление официальное, медстраховка, гибрид 2/3.

Откликаться: @hr_astana_fintech
"""

SCHEMA_HINT = (
    "Return one JSON object and nothing else, with keys: title (string), "
    "company (string or null), city (string or null), salary_min (number or null), "
    "salary_max (number or null), currency (3-letter code or null), "
    "min_years (number or null), skills (array of strings), remote "
    '("no"|"hybrid"|"full"|null), is_vacancy (boolean).'
)

#: A nightly batch, and the token shapes one post costs through an API.
NIGHTLY_POSTS = 200
API_INPUT_TOKENS_PER_POST = 600
API_OUTPUT_TOKENS_PER_POST = 300
#: Below this, "runs overnight" stops being true.
ACCEPTABLE_POSTS_PER_HOUR = 60


async def one_call(client: httpx.AsyncClient) -> tuple[float, int, str]:
    """One post through the local model. Returns seconds, output tokens, text."""
    started = time.perf_counter()
    response = await client.post(
        f"{settings.ollama_base_url.rstrip('/')}/api/chat",
        json={
            "model": settings.ollama_model,
            "messages": [{"role": "user", "content": f"{SCHEMA_HINT}\n\n{SAMPLE_POST}"}],
            "format": "json",
            "stream": False,
        },
        timeout=settings.ollama_timeout,
    )
    response.raise_for_status()
    data = response.json()
    seconds = time.perf_counter() - started
    return (
        seconds,
        int(data.get("eval_count") or 0),
        str((data.get("message") or {}).get("content") or ""),
    )


def api_cost_for(posts: int) -> float | None:
    """What the same batch would cost through the cheapest configured model."""
    priced = [
        (name, price)
        for name, price in settings.llm_pricing.items()
        if price.output_usd_per_mtok > 0
    ]
    if not priced:
        return None
    name, price = min(priced, key=lambda item: item[1].output_usd_per_mtok)
    print(f"api comparison    {name}")
    return (
        posts * API_INPUT_TOKENS_PER_POST * price.input_usd_per_mtok
        + posts * API_OUTPUT_TOKENS_PER_POST * price.output_usd_per_mtok
    ) / MILLION


async def main() -> int:
    """Measure, project, compare, and say what the numbers imply."""
    base = settings.ollama_base_url.rstrip("/")
    print(f"server            {base}")
    print(f"model             {settings.ollama_model}")

    async with httpx.AsyncClient() as client:
        try:
            tags = await client.get(f"{base}/api/tags", timeout=5.0)
            tags.raise_for_status()
        except httpx.HTTPError as exc:
            print(
                f"\nno Ollama at {base} ({type(exc).__name__}).\n"
                "Install it and run:\n"
                f"  ollama pull {settings.ollama_model}\n"
                "then run this again."
            )
            return 2

        names = [model.get("name") for model in (tags.json().get("models") or [])]
        if settings.ollama_model not in names:
            print(
                f"\n{settings.ollama_model} is not pulled.\n"
                f"Available: {sorted(filter(None, names))}\n"
                f"  ollama pull {settings.ollama_model}"
            )
            return 2

        print(f"\nrunning {RUNS} calls...")
        durations: list[float] = []
        outputs: list[int] = []
        sample = ""
        for index in range(RUNS):
            seconds, tokens, text = await one_call(client)
            durations.append(seconds)
            outputs.append(tokens)
            sample = sample or text
            print(f"  {index + 1:>2}  {seconds:6.2f}s  {tokens:>4} tokens")

    median = statistics.median(durations)
    median_tokens = statistics.median(outputs)
    per_hour = 3600 / median if median else 0.0
    nightly_hours = NIGHTLY_POSTS / per_hour if per_hour else float("inf")

    print("\n--- measured --------------------------------------------------")
    print(f"median            {median:.2f}s per post")
    print(f"output tokens     {median_tokens:.0f} median")
    print(f"throughput        {median / max(median_tokens, 1) * 1000:.0f} ms/token")
    print(f"posts per hour    {per_hour:.0f}")
    print(f"{NIGHTLY_POSTS} posts          {nightly_hours:.1f} hours")

    print("\n--- the alternative -------------------------------------------")
    cost = api_cost_for(NIGHTLY_POSTS)
    if cost is not None:
        print(f"{NIGHTLY_POSTS} posts via API  ${cost:.2f} per run, ${cost * 30:.2f} per month")

    print("\n--- what this means -------------------------------------------")
    if per_hour >= ACCEPTABLE_POSTS_PER_HOUR:
        print(
            f"{per_hour:.0f} posts/hour clears the {ACCEPTABLE_POSTS_PER_HOUR}/hour bar. "
            "Keep telegram_parse on ollama."
        )
    else:
        print(
            f"{per_hour:.0f} posts/hour is below the {ACCEPTABLE_POSTS_PER_HOUR}/hour bar, so a\n"
            f"nightly batch takes {nightly_hours:.1f} hours of a busy laptop. Two options:\n"
            "  - a 3B model: two to three times faster, and 'post to JSON' is not a\n"
            "    reasoning task — but check its Russian before trusting it;\n"
            f"  - route telegram_parse to 'api' in LLM_ROUTING"
            + (f" for about ${cost * 30:.2f} a month." if cost is not None else ".")
        )

    print("\n--- one answer, for eyeballing --------------------------------")
    try:
        print(json.dumps(json.loads(sample), ensure_ascii=False, indent=2)[:600])
    except json.JSONDecodeError:
        print("(the model did not return valid JSON — that is itself a result)")
        print(sample[:400])
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

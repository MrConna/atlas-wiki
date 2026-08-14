#!/usr/bin/env python3
"""Opt-in live DeepSeek smoke test. Never prints prompts, evidence, keys, or answers."""

import asyncio
import os
import sys
import time

from app.config import settings
from app.providers import generate_answer


async def main() -> int:
    if os.getenv("RUN_DEEPSEEK_SMOKE") != "1":
        print("SKIP: set RUN_DEEPSEEK_SMOKE=1 to permit a small billable request")
        return 0
    if not settings.deepseek_api_key:
        print("FAIL: DEEPSEEK_API_KEY is not configured", file=sys.stderr)
        return 2
    if settings.model_provider != "deepseek" or not settings.model_name:
        print("FAIL: configure MODEL_PROVIDER=deepseek and MODEL_NAME", file=sys.stderr)
        return 2

    started = time.monotonic()
    answer = await generate_answer(
        "What color is the Atlas smoke fact?",
        ["The Atlas smoke fact is cobalt."],
    )
    elapsed_ms = round((time.monotonic() - started) * 1000)
    if not answer or "[1]" not in answer:
        print(f"FAIL: response did not satisfy the citation contract ({elapsed_ms} ms)", file=sys.stderr)
        return 1
    print(f"PASS: DeepSeek response satisfied the citation contract ({elapsed_ms} ms)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

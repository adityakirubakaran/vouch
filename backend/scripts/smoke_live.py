"""Live check with your real ANTHROPIC_API_KEY.

1. Claude reads each sample scan; fields are compared with the answer key.
2. One full application runs through the workflow with simulated references,
   and the decision is printed.

    make smoke
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vouch.agents.extraction import extract_application  # noqa: E402
from vouch.config import Settings  # noqa: E402
from vouch.db import Application  # noqa: E402
from vouch.fixtures_data import sample_path, samples  # noqa: E402
from vouch.llm import AnthropicLLM  # noqa: E402
from vouch.schemas import CreditApplication  # noqa: E402
from vouch.services import build_services, start_services, stop_services  # noqa: E402
from vouch.util import normalize_phone  # noqa: E402


def flatten(prefix: str, value, out: dict) -> None:
    if isinstance(value, dict):
        for k, v in value.items():
            flatten(f"{prefix}.{k}" if prefix else k, v, out)
    elif isinstance(value, list):
        for i, v in enumerate(value):
            flatten(f"{prefix}.{i}", v, out)
    else:
        out[prefix] = value


def comparable(key: str, value):
    if value is None:
        return None
    if "phone" in key:
        return normalize_phone(str(value))
    if isinstance(value, str):
        return " ".join(value.lower().replace(",", "").split())
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


async def check_extraction(settings: Settings) -> bool:
    llm = AnthropicLLM(settings)
    ok = True
    for sample in samples():
        started = time.time()
        got = await extract_application(llm, settings.seller_name, sample_path(sample).read_bytes(), "application/pdf")
        want = CreditApplication.model_validate(sample["application"])
        a, b = {}, {}
        flatten("", want.model_dump(exclude={"issues"}), a)
        flatten("", got.model_dump(exclude={"issues"}), b)
        misses = [(k, a[k], b.get(k)) for k in a if comparable(k, a[k]) != comparable(k, b.get(k))]
        print(f"\n{sample['title']}: {len(a) - len(misses)}/{len(a)} fields match ({time.time() - started:.1f}s)")
        for k, want_v, got_v in misses:
            print(f"  {k}: expected {want_v!r}, got {got_v!r}")
        ok = ok and len(misses) <= 2
    return ok


async def run_application(settings: Settings) -> None:
    svc = build_services(settings)
    await start_services(svc)
    try:
        path = sample_path(samples()[0])
        app_id = svc.workflow.create_application(path.name, path.read_bytes(), sample_id="acme")  # type: ignore[union-attr]
        print(f"\nRunning {path.name} end to end (simulated references, takes a few minutes)...")
        last = None
        deadline = time.time() + 900
        while time.time() < deadline:
            with svc.db.session() as s:
                app = s.get(Application, app_id)
                status, decision, error = app.status, app.decision, app.error
            if status != last:
                print(f"  {status}")
                last = status
            if status in ("ready_for_review", "failed"):
                break
            await asyncio.sleep(1)
        if decision:
            print(f"\nDecision: {decision['decision']}, limit {decision.get('recommended_limit')}, terms {decision['terms']}")
            print(f"Headline: {decision['narrative']['headline']}")
        elif error:
            print(f"\nFailed: {error}")
    finally:
        await stop_services(svc)


async def main() -> None:
    settings = Settings(start_engine=False, call_channel="simulated", call_retry_seconds=5, honor_callback_times=False,
                        data_dir=Path(__file__).resolve().parents[2] / "data" / "smoke")
    if not settings.anthropic_api_key:
        print("Set ANTHROPIC_API_KEY in .env first.")
        sys.exit(1)
    print(f"Models: smart={settings.model_smart} fast={settings.model_fast}")
    ok = await check_extraction(settings)
    await run_application(settings)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    asyncio.run(main())

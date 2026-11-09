from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from vouch.config import Settings
from vouch.services import Services, build_services, start_services, stop_services


def make_settings(tmp_path: Path, **overrides) -> Settings:
    base = dict(
        _env_file=None,
        data_dir=tmp_path,
        vouch_llm="fake",
        anthropic_api_key=None,
        call_channel="simulated",
        call_retry_seconds=0.2,
        honor_callback_times=False,  # tests call back right away instead of "Thursday"
        real_call_limit=0,  # tests put every reference on the configured line unless they say otherwise
        call_ring_timeout_seconds=5,
        simulate_bank_reply_seconds=0.3,
        bank_reply_timeout_seconds=30,
        live_domain_checks=False,
        sim_time_scale=0.01,
        engine_poll_seconds=0.05,
        start_engine=False,
        validate_twilio_signatures=True,
    )
    base.update(overrides)
    return Settings(**base)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return make_settings(tmp_path)


@pytest.fixture
async def services(settings: Settings):
    svc = build_services(settings)
    await start_services(svc)
    yield svc
    await stop_services(svc)


async def wait_for_status(svc: Services, app_id: str, statuses: tuple[str, ...], timeout: float = 20.0) -> str:
    from vouch.db import Application

    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        with svc.db.session() as s:
            app = s.get(Application, app_id)
            status = app.status if app else None
        if status in statuses:
            return status
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"application stuck in {status!r}, wanted {statuses}")
        await asyncio.sleep(0.05)

"""The durable workflow engine on its own, with toy handlers."""

from __future__ import annotations

import asyncio

from vouch.db import Application, Database, Step
from vouch.engine import Done, Engine, Retry, Skip, StepFailed, Wait
from vouch.events import EventBus


def setup_engine(tmp_path, **kw):
    db = Database(f"sqlite:///{tmp_path / 'e.db'}")
    db.create_all()
    bus = EventBus(db)
    engine = Engine(db, bus, poll_seconds=0.02, **kw)
    with db.session() as s:
        s.add(Application(id="app_1", source_filename="x.pdf", source_path="x.pdf"))
    return db, engine


def add(db, engine, kind, **kw) -> str:
    with db.session() as s:
        return engine.add_step(s, "app_1", kind, **kw).id


def status(db, step_id) -> str:
    with db.session() as s:
        return s.get(Step, step_id).status


async def test_wait_then_signal_resumes_with_payload(tmp_path):
    db, engine = setup_engine(tmp_path)
    seen = []

    async def handler(ctx):
        if ctx.signal:
            seen.append(ctx.signal)
            return Done({"got": ctx.signal["x"]})
        return Wait("waiting for x")

    engine.register("w", handler)
    sid = add(db, engine, "w")
    await engine.run_until_idle()
    assert status(db, sid) == "waiting"
    assert engine.signal(sid, {"x": 42})
    await engine.run_until_idle()
    assert status(db, sid) == "done" and seen == [{"x": 42}]
    assert not engine.signal(sid, {"x": 1})  # no longer waiting


async def test_wait_timeout_resumes_with_timeout_flag(tmp_path):
    db, engine = setup_engine(tmp_path)

    async def handler(ctx):
        return Done({"timeout": ctx.resumed_by_timeout}) if ctx.signal else Wait("slow", timeout_seconds=0.05)

    engine.register("t", handler)
    sid = add(db, engine, "t")
    await engine.run_until_idle()
    await asyncio.sleep(0.1)
    await engine.run_until_idle()
    with db.session() as s:
        step = s.get(Step, sid)
        assert step.status == "done" and step.result == {"timeout": True}


async def test_retry_runs_again_later_and_keeps_state(tmp_path):
    db, engine = setup_engine(tmp_path)

    async def handler(ctx):
        ctx.state["n"] = ctx.state.get("n", 0) + 1
        return Done({"n": ctx.state["n"]}) if ctx.state["n"] >= 3 else Retry(0.01, "again")

    engine.register("r", handler)
    sid = add(db, engine, "r")
    for _ in range(10):
        await engine.run_until_idle()
        await asyncio.sleep(0.02)
        if status(db, sid) == "done":
            break
    with db.session() as s:
        assert s.get(Step, sid).result == {"n": 3}


async def test_exceptions_retry_then_fail(tmp_path):
    db, engine = setup_engine(tmp_path, max_attempts=2)
    calls = []

    async def handler(ctx):
        calls.append(ctx.attempts)
        raise RuntimeError("boom")

    engine.register("x", handler)
    sid = add(db, engine, "x")
    for _ in range(20):
        await engine.run_until_idle()
        await asyncio.sleep(0.05)
        if status(db, sid) == "failed":
            break
    assert status(db, sid) == "failed" and calls == [1, 2]


async def test_dependencies_and_required_failure(tmp_path):
    db, engine = setup_engine(tmp_path)
    order = []

    async def ok(ctx):
        order.append(ctx.kind)
        return Done()

    async def bad(ctx):
        raise StepFailed("nope")

    async def skip(ctx):
        return Skip("not needed")

    engine.register("a", ok)
    engine.register("b", bad)
    engine.register("c", ok)
    engine.register("s", skip)
    a = add(db, engine, "a")
    b = add(db, engine, "b")
    s = add(db, engine, "s")
    c_soft = add(db, engine, "c", depends_on=[a, b, s])  # failed and skipped deps are fine
    c_hard = add(db, engine, "c", depends_on=[a, b], state={"requires": [b]})
    await engine.run_until_idle()
    assert status(db, b) == "failed" and status(db, s) == "skipped"
    assert status(db, c_soft) == "done"
    assert status(db, c_hard) == "skipped"


async def test_recover_resets_running_steps(tmp_path):
    db, engine = setup_engine(tmp_path)
    sid = add(db, engine, "z")
    with db.session() as s:
        s.get(Step, sid).status = "running"
    assert engine.recover() == 1
    assert status(db, sid) == "pending"


async def test_run_now_pulls_retry_forward(tmp_path):
    db, engine = setup_engine(tmp_path)

    async def handler(ctx):
        if ctx.state.get("go"):
            return Done()
        return Retry(3600, "later")

    engine.register("p", handler)
    sid = add(db, engine, "p")
    await engine.run_until_idle()
    assert status(db, sid) == "pending"
    engine.run_now(sid, {"go": True})
    await engine.run_until_idle()
    assert status(db, sid) == "done"

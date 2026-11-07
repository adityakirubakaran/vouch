"""A small durable workflow engine.

Each credit application is a graph of steps stored in SQLite. The engine polls
for steps that are due, runs their handler, and records the outcome:

  Done(result)        step finished; dependents may start
  Wait(reason, t)     park until signal() delivers a payload or t seconds pass
  Retry(after, why)   run the handler again later (e.g. reference did not pick up)
  Skip(why)           not needed (e.g. a flagged reference we will not call)

Because all state lives in the database, a restart resumes where it left off:
steps that were mid-run go back to pending, waiting steps keep waiting, and
scheduled retries keep their times. Handlers are written to be idempotent.
"""

from __future__ import annotations

import asyncio
import logging
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Optional, Union

from sqlalchemy import or_, select

from .db import Database, Step, utcnow
from .events import EventBus

log = logging.getLogger("vouch.engine")

TERMINAL = ("done", "skipped", "failed")


@dataclass
class Done:
    result: dict = field(default_factory=dict)


@dataclass
class Wait:
    reason: str
    timeout_seconds: Optional[float] = None


@dataclass
class Retry:
    after_seconds: float
    reason: str


@dataclass
class Skip:
    reason: str


Outcome = Union[Done, Wait, Retry, Skip]


class StepFailed(Exception):
    """Raise from a handler to fail a step immediately, without retries."""


@dataclass
class StepContext:
    step_id: str
    kind: str
    application_id: str
    reference_id: Optional[str]
    attempts: int
    state: dict
    signal: Optional[dict]
    engine: "Engine"

    @property
    def resumed_by_timeout(self) -> bool:
        return bool(self.signal and self.signal.get("timeout"))


Handler = Callable[[StepContext], Awaitable[Outcome]]
AfterHook = Callable[[Any, Step], None]  # (session, step) for done, skipped and failed steps


class Engine:
    def __init__(
        self,
        db: Database,
        bus: EventBus,
        handlers: Optional[dict[str, Handler]] = None,
        after_step: Optional[AfterHook] = None,
        poll_seconds: float = 0.5,
        max_concurrency: int = 8,
        max_attempts: int = 3,
    ) -> None:
        self.db = db
        self.bus = bus
        self.handlers: dict[str, Handler] = handlers or {}
        self.after_step = after_step
        self.poll_seconds = poll_seconds
        self.max_attempts = max_attempts
        self._sem = asyncio.Semaphore(max_concurrency)
        self._running: set[str] = set()
        self._tasks: set[asyncio.Task] = set()
        self._wake = asyncio.Event()
        self._loop_task: Optional[asyncio.Task] = None
        self._stopping = False

    # ------------------------------------------------------------------ setup

    def register(self, kind: str, handler: Handler) -> None:
        self.handlers[kind] = handler

    async def start(self) -> None:
        self.recover()
        self._stopping = False
        self._loop_task = asyncio.create_task(self._loop(), name="vouch-engine")

    async def stop(self) -> None:
        self._stopping = True
        self.wake()
        if self._loop_task:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass
        for t in list(self._tasks):
            t.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    def recover(self) -> int:
        """Steps marked running belonged to a process that died; run them again."""
        with self.db.session() as s:
            rows = s.scalars(select(Step).where(Step.status == "running")).all()
            for row in rows:
                row.status = "pending"
                row.next_run_at = None
            return len(rows)

    def wake(self) -> None:
        self._wake.set()

    # ------------------------------------------------------------ public API

    def add_step(
        self,
        session: Any,
        application_id: str,
        kind: str,
        reference_id: Optional[str] = None,
        depends_on: Optional[list[str]] = None,
        state: Optional[dict] = None,
        run_at: Optional[datetime] = None,
    ) -> Step:
        step = Step(
            application_id=application_id,
            kind=kind,
            reference_id=reference_id,
            depends_on=list(depends_on or []),
            state=dict(state or {}),
            next_run_at=run_at,
        )
        session.add(step)
        session.flush()
        self.wake()
        return step

    def signal(self, step_id: str, payload: dict) -> bool:
        """Deliver an external event (call ended, email arrived, human action) to a
        waiting step. Returns False if the step is not waiting."""
        with self.db.session() as s:
            step = s.get(Step, step_id)
            if step is None or step.status != "waiting":
                return False
            step.signal = payload
            step.status = "pending"
            step.next_run_at = None
            step.timeout_at = None
            step.wait_reason = None
            app_id, kind, ref_id = step.application_id, step.kind, step.reference_id
        self._publish_step(step_id, app_id, kind, ref_id, "pending")
        self.wake()
        return True

    def run_now(self, step_id: str, state_patch: Optional[dict] = None) -> bool:
        """Pull a scheduled retry forward (the "Call now" button)."""
        with self.db.session() as s:
            step = s.get(Step, step_id)
            if step is None or step.status not in ("pending", "waiting"):
                return False
            if state_patch:
                step.state = {**(step.state or {}), **state_patch}
            if step.status == "waiting":
                step.signal = {"run_now": True}
                step.status = "pending"
            step.next_run_at = None
            step.timeout_at = None
            app_id, kind, ref_id = step.application_id, step.kind, step.reference_id
        self._publish_step(step_id, app_id, kind, ref_id, "pending")
        self.wake()
        return True

    async def run_until_idle(self, timeout: float = 30.0, settle: float = 0.05) -> None:
        """Drive the engine without the background loop (tests). Returns when no
        step is runnable and nothing is in flight."""
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            started = self._dispatch_due()
            if self._tasks:
                await asyncio.wait(self._tasks, timeout=settle)
            elif not started:
                await asyncio.sleep(settle)
                if not self._dispatch_due() and not self._tasks:
                    return
            if asyncio.get_running_loop().time() > deadline:
                raise TimeoutError("engine did not go idle")

    # --------------------------------------------------------------- internals

    async def _loop(self) -> None:
        while not self._stopping:
            try:
                self._dispatch_due()
            except Exception:  # pragma: no cover - keep the loop alive
                log.exception("engine dispatch failed")
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.poll_seconds)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()

    def _dispatch_due(self) -> int:
        now = utcnow()
        started = 0
        with self.db.session() as s:
            # waiting steps whose timeout passed resume with a timeout signal
            expired = s.scalars(
                select(Step).where(Step.status == "waiting", Step.timeout_at.is_not(None), Step.timeout_at <= now)
            ).all()
            for step in expired:
                step.status = "pending"
                step.signal = {"timeout": True}
                step.timeout_at = None

            candidates = s.scalars(
                select(Step)
                .where(Step.status == "pending", or_(Step.next_run_at.is_(None), Step.next_run_at <= now))
                .order_by(Step.created_at)
            ).all()
            to_run: list[str] = []
            for step in candidates:
                if step.id in self._running:
                    continue
                ready, blocked = self._deps_state(s, step)
                if blocked:
                    step.status = "skipped"
                    step.error = blocked
                    self._publish_step(step.id, step.application_id, step.kind, step.reference_id, "skipped")
                    continue
                if not ready:
                    continue
                step.status = "running"
                step.attempts += 1
                to_run.append(step.id)
        for step_id in to_run:
            self._running.add(step_id)
            task = asyncio.create_task(self._execute(step_id))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            started += 1
        return started

    def _deps_state(self, s: Any, step: Step) -> tuple[bool, Optional[str]]:
        requires = set((step.state or {}).get("requires", []))
        for dep_id in step.depends_on or []:
            dep = s.get(Step, dep_id)
            if dep is None:
                continue
            if dep.status not in TERMINAL:
                return False, None
            if dep_id in requires and dep.status != "done":
                return False, f"required step {dep.kind} did not finish"
        return True, None

    async def _execute(self, step_id: str) -> None:
        async with self._sem:
            with self.db.session() as s:
                step = s.get(Step, step_id)
                assert step is not None
                ctx = StepContext(
                    step_id=step.id,
                    kind=step.kind,
                    application_id=step.application_id,
                    reference_id=step.reference_id,
                    attempts=step.attempts,
                    state=dict(step.state or {}),
                    signal=step.signal,
                    engine=self,
                )
                app_id, kind, ref_id = step.application_id, step.kind, step.reference_id
            self._publish_step(step_id, app_id, kind, ref_id, "running")
            handler = self.handlers.get(kind)
            try:
                if handler is None:
                    raise StepFailed(f"no handler for step kind {kind}")
                outcome = await handler(ctx)
                self._record(step_id, ctx, outcome)
            except StepFailed as exc:
                self._fail(step_id, ctx, str(exc))
            except Exception as exc:  # noqa: BLE001
                log.error("step %s (%s) raised: %s\n%s", step_id, kind, exc, traceback.format_exc())
                max_attempts = ctx.state.get("max_attempts", self.max_attempts)
                if ctx.attempts < max_attempts:
                    self._record(step_id, ctx, Retry(after_seconds=2 ** ctx.attempts, reason=f"error: {exc}"))
                else:
                    self._fail(step_id, ctx, f"{type(exc).__name__}: {exc}")
            finally:
                self._running.discard(step_id)
                self.wake()

    def _record(self, step_id: str, ctx: StepContext, outcome: Outcome) -> None:
        now = utcnow()
        done_step: Optional[Step] = None
        with self.db.session() as s:
            step = s.get(Step, step_id)
            assert step is not None
            step.state = ctx.state
            step.signal = None
            step.error = None
            if isinstance(outcome, Done):
                step.status = "done"
                step.result = outcome.result
                step.wait_reason = None
            elif isinstance(outcome, Skip):
                step.status = "skipped"
                step.result = {"reason": outcome.reason}
                step.wait_reason = outcome.reason
            elif isinstance(outcome, Wait):
                step.status = "waiting"
                step.wait_reason = outcome.reason
                step.timeout_at = now + timedelta(seconds=outcome.timeout_seconds) if outcome.timeout_seconds else None
            elif isinstance(outcome, Retry):
                step.status = "pending"
                step.wait_reason = outcome.reason
                step.next_run_at = now + timedelta(seconds=outcome.after_seconds)
            status = step.status
            if status in ("done", "skipped"):
                done_step = step
            app_id, kind, ref_id = step.application_id, step.kind, step.reference_id
            extra = {
                "wait_reason": step.wait_reason,
                "next_run_at": step.next_run_at.isoformat() if step.next_run_at and status == "pending" else None,
            }
            if done_step is not None and self.after_step is not None:
                self.after_step(s, done_step)
        self._publish_step(step_id, app_id, kind, ref_id, status, extra)

    def _fail(self, step_id: str, ctx: StepContext, error: str) -> None:
        with self.db.session() as s:
            step = s.get(Step, step_id)
            assert step is not None
            step.state = ctx.state
            step.status = "failed"
            step.error = error
            app_id, kind, ref_id = step.application_id, step.kind, step.reference_id
            if self.after_step is not None:
                self.after_step(s, step)
        self._publish_step(step_id, app_id, kind, ref_id, "failed", {"error": error})

    def _publish_step(
        self,
        step_id: str,
        application_id: str,
        kind: str,
        reference_id: Optional[str],
        status: str,
        extra: Optional[dict] = None,
    ) -> None:
        self.bus.publish(
            "step.updated",
            f"{kind} {status}",
            application_id=application_id,
            data={"step_id": step_id, "kind": kind, "reference_id": reference_id, "status": status, **(extra or {})},
            persist=False,
        )

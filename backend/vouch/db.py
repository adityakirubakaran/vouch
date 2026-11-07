"""SQLite persistence. Every piece of workflow state lives here so the engine can
resume after a restart."""

from __future__ import annotations

import json
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator, Optional

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    TypeDecorator,
    create_engine,
    event,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


class Base(DeclarativeBase):
    type_annotation_map = {dict: JSON, list: JSON}


class TZDateTime(TypeDecorator):
    """Store UTC as naive datetimes (SQLite has no tz) and hand back aware ones."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: Optional[datetime], dialect: Any) -> Optional[datetime]:
        if value is None:
            return None
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc).replace(tzinfo=None)
        return value

    def process_result_value(self, value: Optional[datetime], dialect: Any) -> Optional[datetime]:
        if value is None:
            return None
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


class Application(Base):
    __tablename__ = "applications"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: new_id("app"))
    created_at: Mapped[datetime] = mapped_column(TZDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(TZDateTime(), default=utcnow, onupdate=utcnow)
    status: Mapped[str] = mapped_column(String, default="received")
    source_filename: Mapped[str] = mapped_column(String)
    source_path: Mapped[str] = mapped_column(String)
    sample_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    applicant_name: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    requested_limit: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    requested_terms: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    extracted: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    applicant_checks: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    decision: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)  # policy result + narrative
    review: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)  # human action
    sync: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)  # accounting sync result
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    references: Mapped[list["Reference"]] = relationship(back_populates="application", cascade="all, delete-orphan", order_by="Reference.position")
    steps: Mapped[list["Step"]] = relationship(back_populates="application", cascade="all, delete-orphan", order_by="Step.created_at")


class Reference(Base):
    __tablename__ = "references"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: new_id("ref"))
    application_id: Mapped[str] = mapped_column(ForeignKey("applications.id"))
    position: Mapped[int] = mapped_column(Integer, default=0)
    kind: Mapped[str] = mapped_column(String)  # trade | bank
    company: Mapped[str] = mapped_column(String)
    contact_name: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    phone: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    email: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    details: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String, default="pending")
    checks: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)  # verification result
    flagged: Mapped[bool] = mapped_column(Boolean, default=False)
    findings: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    outcome: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    application: Mapped[Application] = relationship(back_populates="references")
    calls: Mapped[list["Call"]] = relationship(back_populates="reference", cascade="all, delete-orphan", order_by="Call.created_at")


class Step(Base):
    """One node in an application's workflow graph."""

    __tablename__ = "steps"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: new_id("step"))
    application_id: Mapped[str] = mapped_column(ForeignKey("applications.id"))
    kind: Mapped[str] = mapped_column(String)  # extract | verify | call | bank | memo | sync
    reference_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, default="pending")
    # pending -> running -> done | waiting | failed | skipped
    depends_on: Mapped[list] = mapped_column(JSON, default=list)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_run_at: Mapped[Optional[datetime]] = mapped_column(TZDateTime(), nullable=True)
    timeout_at: Mapped[Optional[datetime]] = mapped_column(TZDateTime(), nullable=True)
    wait_reason: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    state: Mapped[dict] = mapped_column(JSON, default=dict)  # handler-private state between runs
    signal: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)  # payload delivered while waiting
    result: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TZDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(TZDateTime(), default=utcnow, onupdate=utcnow)

    application: Mapped[Application] = relationship(back_populates="steps")


class Call(Base):
    __tablename__ = "calls"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: new_id("call"))
    reference_id: Mapped[str] = mapped_column(ForeignKey("references.id"))
    application_id: Mapped[str] = mapped_column(String)
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    channel: Mapped[str] = mapped_column(String)  # simulated | browser | twilio
    dialed_number: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, default="queued")
    # queued, ringing, in_progress, ended
    outcome: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    provider_sid: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    transcript: Mapped[list] = mapped_column(JSON, default=list)  # [{role, text, ts}]
    live_findings: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    result: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    ring_deadline: Mapped[Optional[datetime]] = mapped_column(TZDateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TZDateTime(), default=utcnow)
    started_at: Mapped[Optional[datetime]] = mapped_column(TZDateTime(), nullable=True)
    ended_at: Mapped[Optional[datetime]] = mapped_column(TZDateTime(), nullable=True)

    reference: Mapped[Reference] = relationship(back_populates="calls")


class Email(Base):
    __tablename__ = "emails"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: new_id("email"))
    application_id: Mapped[str] = mapped_column(String)
    reference_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    direction: Mapped[str] = mapped_column(String)  # outbound | inbound
    to_addr: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    from_addr: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    subject: Mapped[str] = mapped_column(String)
    body: Mapped[str] = mapped_column(Text)
    delivery: Mapped[str] = mapped_column(String, default="outbox")  # outbox | smtp | simulated | webhook
    created_at: Mapped[datetime] = mapped_column(TZDateTime(), default=utcnow)


class Evidence(Base):
    """A citable fact. Memo sentences point at these by id."""

    __tablename__ = "evidence"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    application_id: Mapped[str] = mapped_column(String)
    kind: Mapped[str] = mapped_column(String)  # call | bank | registry | domain | fraud | application
    label: Mapped[str] = mapped_column(String)
    detail: Mapped[str] = mapped_column(Text)
    source_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)  # call/email/reference id
    created_at: Mapped[datetime] = mapped_column(TZDateTime(), default=utcnow)


class EventRow(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    application_id: Mapped[Optional[str]] = mapped_column(String, nullable=True, index=True)
    ts: Mapped[datetime] = mapped_column(TZDateTime(), default=utcnow)
    type: Mapped[str] = mapped_column(String)
    message: Mapped[str] = mapped_column(Text)
    data: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)


class KV(Base):
    """Small key-value store (OAuth tokens, ledger sequence)."""

    __tablename__ = "kv"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(Text)


class Database:
    def __init__(self, url: str) -> None:
        connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
        self.engine: Engine = create_engine(url, connect_args=connect_args, future=True)
        if url.startswith("sqlite"):

            @event.listens_for(self.engine, "connect")
            def _pragmas(dbapi_conn: Any, _rec: Any) -> None:
                cur = dbapi_conn.cursor()
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA busy_timeout=5000")
                cur.execute("PRAGMA foreign_keys=ON")
                cur.close()

        self._factory = sessionmaker(bind=self.engine, expire_on_commit=False, future=True)

    def create_all(self) -> None:
        Base.metadata.create_all(self.engine)

    @contextmanager
    def session(self) -> Iterator[Session]:
        s = self._factory()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    # KV helpers
    def kv_get(self, key: str) -> Optional[Any]:
        with self.session() as s:
            row = s.get(KV, key)
            return json.loads(row.value) if row else None

    def kv_set(self, key: str, value: Any) -> None:
        with self.session() as s:
            row = s.get(KV, key)
            if row:
                row.value = json.dumps(value)
            else:
                s.add(KV(key=key, value=json.dumps(value)))

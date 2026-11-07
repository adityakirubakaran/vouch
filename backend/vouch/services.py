"""Wires settings, storage, Claude, providers, and the engine together."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from .config import Settings
from .db import Database
from .engine import Engine
from .events import EventBus
from .llm import LLM, build_llm
from .providers.records import DomainProvider, RegistryProvider, WebsiteProvider

if TYPE_CHECKING:  # pragma: no cover
    from .calls import CallManager
    from .providers.accounting import Accounting
    from .providers.mail import Mailer
    from .workflow import Workflow


@dataclass
class Services:
    settings: Settings
    db: Database
    bus: EventBus
    llm: LLM
    engine: Engine
    registry: RegistryProvider
    domains: DomainProvider
    websites: WebsiteProvider
    calls: "CallManager"
    mailer: "Mailer"
    accounting: "Accounting"
    workflow: Optional["Workflow"] = None


def build_services(settings: Settings, llm: Optional[LLM] = None) -> Services:
    from .calls import CallManager
    from .providers.accounting import build_accounting
    from .providers.mail import Mailer
    from .workflow import Workflow

    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.uploads_dir.mkdir(parents=True, exist_ok=True)
    db = Database(settings.db_url)
    db.create_all()
    bus = EventBus(db)
    engine = Engine(db, bus, poll_seconds=settings.engine_poll_seconds)
    services = Services(
        settings=settings,
        db=db,
        bus=bus,
        llm=llm or build_llm(settings),
        engine=engine,
        registry=RegistryProvider(settings),
        domains=DomainProvider(settings),
        websites=WebsiteProvider(settings),
        calls=None,  # type: ignore[arg-type]
        mailer=None,  # type: ignore[arg-type]
        accounting=None,  # type: ignore[arg-type]
    )
    services.calls = CallManager(services)
    services.mailer = Mailer(services)
    services.accounting = build_accounting(services)
    Workflow(services).install()
    return services


async def start_services(services: Services) -> None:
    """Recover anything a previous process left mid-flight, then start the engine."""
    services.engine.recover()
    services.calls.recover_orphans()
    await services.engine.start()


async def stop_services(services: Services) -> None:
    await services.engine.stop()
    await services.calls.shutdown()

"""Runtime settings, read from environment variables or a .env file at the repo root."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal, Optional

from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(str(REPO_ROOT / ".env"),),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Who is extending credit. Shown to references on calls and in emails.
    seller_name: str = "Northline Supply"
    seller_contact_email: str = "credit@northlinesupply.example"

    # Claude
    anthropic_api_key: Optional[str] = None
    vouch_llm: Literal["auto", "anthropic", "fake"] = "auto"
    model_smart: str = "claude-sonnet-5-5"  # extraction, memo, transcript parsing
    model_fast: str = "claude-haiku-5-5"  # live voice turns, live field capture

    # Where things go
    data_dir: Path = REPO_ROOT / "data"
    database_url: Optional[str] = None
    public_base_url: Optional[str] = None  # https URL Twilio can reach (cloudflared/ngrok)

    # Phone calls: simulated (Claude plays the reference), browser (a person answers
    # in the /phone page), or twilio (real phone call).
    call_channel: Literal["simulated", "browser", "twilio"] = "simulated"
    twilio_account_sid: Optional[str] = None
    twilio_auth_token: Optional[str] = None
    twilio_from_number: Optional[str] = None
    twilio_tts_provider: str = "ElevenLabs"
    twilio_voice: Optional[str] = None
    twilio_amd: bool = True
    # Route every reference call to this number during a live demo, while the
    # memo still shows the number written on the application.
    demo_reference_phone: Optional[str] = None
    validate_twilio_signatures: bool = True

    # Retry schedule for references that do not pick up.
    call_max_attempts: int = 3
    call_retry_seconds: float = 30  # demo-friendly; use hours in production
    call_ring_timeout_seconds: float = 45
    honor_callback_times: bool = False  # demo: retry soon even if "call back Thursday"

    # Email to the customer's bank: outbox (shown in the app) or smtp.
    email_mode: Literal["outbox", "smtp"] = "outbox"
    smtp_host: Optional[str] = None
    smtp_port: int = 587
    smtp_username: Optional[str] = None
    smtp_password: Optional[str] = None
    smtp_from: Optional[str] = None
    simulate_bank_reply_seconds: float = 8  # 0 disables the simulated bank reply
    bank_reply_timeout_seconds: float = 600

    # Business registry: sandbox fixtures for the demo companies, or OpenCorporates.
    registry_mode: Literal["sandbox", "opencorporates"] = "sandbox"
    opencorporates_api_token: Optional[str] = None
    live_domain_checks: bool = True  # RDAP lookups for real domains

    # Accounting sync: a local ledger file, or a QuickBooks Online sandbox company.
    accounting_mode: Literal["ledger", "quickbooks"] = "ledger"
    quickbooks_client_id: Optional[str] = None
    quickbooks_client_secret: Optional[str] = None
    quickbooks_environment: Literal["sandbox", "production"] = "sandbox"

    # Speeds up simulated calls (rings, pauses) in tests; 1.0 is real time.
    sim_time_scale: float = 1.0

    # Engine
    engine_poll_seconds: float = 0.5
    start_engine: bool = True

    @property
    def llm_mode(self) -> str:
        if self.vouch_llm == "auto":
            return "anthropic" if self.anthropic_api_key else "fake"
        return self.vouch_llm

    @property
    def db_url(self) -> str:
        if self.database_url:
            return self.database_url
        return f"sqlite:///{self.data_dir / 'vouch.db'}"

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def twilio_configured(self) -> bool:
        return bool(self.twilio_account_sid and self.twilio_auth_token and self.twilio_from_number)


@lru_cache
def get_settings() -> Settings:
    return Settings()

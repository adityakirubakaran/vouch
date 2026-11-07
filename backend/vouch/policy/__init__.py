"""Credit policy loading."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

POLICY_PATH = Path(__file__).resolve().parent / "credit_policy.yaml"


@lru_cache
def load_policy() -> dict:
    return yaml.safe_load(POLICY_PATH.read_text())

"""Entry point: `python -m vouch.main` or `uvicorn vouch.main:app`."""

from __future__ import annotations

import os

import uvicorn

from .api.app import create_app

app = create_app()


def run() -> None:
    uvicorn.run(
        "vouch.main:app",
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8000")),
        reload=os.environ.get("RELOAD", "0") == "1",
    )


if __name__ == "__main__":
    run()

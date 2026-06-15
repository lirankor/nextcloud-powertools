"""Shared pytest fixtures: a base Settings + a NextcloudClient bound to a base URL."""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from ncpowertools.config import Settings
from ncpowertools.handlers.base import HandlerContext
from ncpowertools.models import FileRef
from ncpowertools.nextcloud import NextcloudClient

BASE_URL = "https://cloud.example.com"
USER = "powertools"


@pytest.fixture
def make_ctx(tmp_path: Path) -> Callable[..., HandlerContext]:
    """Factory building a HandlerContext for a given source name.

    Usage: ``ctx = make_ctx("foo.zip", is_dir=False, max_files=..., ...)``.
    The work_dir is a unique tmp subdir per context.
    """
    counter = {"n": 0}

    def _make(
        name: str,
        *,
        is_dir: bool = False,
        fileid: int = 1,
        max_uncompressed_size: int = 2_147_483_648,
        max_files: int = 10_000,
        enable_rar: bool = False,
    ) -> HandlerContext:
        counter["n"] += 1
        work = tmp_path / f"work{counter['n']}"
        work.mkdir(parents=True, exist_ok=True)
        return HandlerContext(
            work_dir=work,
            src=FileRef(fileid=fileid, path=name, is_dir=is_dir, name=name),
            max_uncompressed_size=max_uncompressed_size,
            max_files=max_files,
            enable_rar=enable_rar,
            logger=logging.getLogger("test.handler"),
        )

    return _make


@pytest.fixture
def settings() -> Settings:
    return Settings(
        NEXTCLOUD_URL=BASE_URL,
        NC_USER=USER,
        NC_APP_PASSWORD="app-pw",
    )


@pytest.fixture
def client(settings: Settings) -> NextcloudClient:
    # A real httpx.Client; respx intercepts its transport in tests.
    c = NextcloudClient(settings)
    yield c
    c.close()


@pytest.fixture
def capabilities_json() -> dict[str, object]:
    return {
        "ocs": {
            "meta": {"status": "ok", "statuscode": 200},
            "data": {"version": {"major": 33, "minor": 0, "micro": 1, "string": "33.0.1"}},
        }
    }


def make_response(status: int, *, text: str = "", headers: dict[str, str] | None = None):
    """Build an httpx.Response for respx side_effects/return values."""
    return httpx.Response(status, text=text, headers=headers or {})

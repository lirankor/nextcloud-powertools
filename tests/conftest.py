"""Shared pytest fixtures: a base Settings + a NextcloudClient bound to a base URL."""

from __future__ import annotations

import httpx
import pytest

from ncpowertools.config import Settings
from ncpowertools.nextcloud import NextcloudClient

BASE_URL = "https://cloud.example.com"
USER = "powertools"


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

"""Tests for config loading and TAG_ACTIONS parsing."""

from __future__ import annotations

import pytest

from ncpowertools.config import DEFAULT_TAG_ACTIONS, Settings, load_settings, parse_tag_actions
from ncpowertools.errors import ConfigError


def test_required_vars_missing_raises_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("NEXTCLOUD_URL", "NC_USER", "NC_APP_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
    # Avoid picking up a developer .env.
    monkeypatch.setattr("ncpowertools.config.Settings.model_config", {"env_file": None})
    with pytest.raises(ConfigError) as exc:
        load_settings(_env_file=None)
    msg = str(exc.value)
    assert "Missing required configuration" in msg
    assert "NEXTCLOUD_URL" in msg


def test_defaults_applied() -> None:
    s = Settings(NEXTCLOUD_URL="https://x/", NC_USER="svc", NC_APP_PASSWORD="pw")
    assert s.NEXTCLOUD_URL == "https://x"  # trailing slash stripped
    assert s.NC_ADMIN_USER == "svc"
    assert s.NC_ADMIN_PASSWORD == "pw"
    assert s.TARGET_USER == "svc"
    assert s.POLL_INTERVAL == 60
    assert s.MAX_UNCOMPRESSED_SIZE == 2147483648
    assert s.MAX_FILES == 10000
    assert s.ERROR_TAG == "powertools-error"
    assert s.NOTIFY is False
    assert s.TAG_ACTIONS == DEFAULT_TAG_ACTIONS


def test_admin_target_overrides() -> None:
    s = Settings(
        NEXTCLOUD_URL="https://x",
        NC_USER="svc",
        NC_APP_PASSWORD="pw",
        NC_ADMIN_USER="admin",
        NC_ADMIN_PASSWORD="adminpw",
        TARGET_USER="alice",
    )
    assert s.NC_ADMIN_USER == "admin"
    assert s.NC_ADMIN_PASSWORD == "adminpw"
    assert s.TARGET_USER == "alice"


def test_tag_actions_json_form() -> None:
    parsed = parse_tag_actions('{"extract": "extract", "z": "zip"}')
    assert parsed == {"extract": "extract", "z": "zip"}


def test_tag_actions_compact_form() -> None:
    parsed = parse_tag_actions("extract:extract, zip:zip , render:render")
    assert parsed == {"extract": "extract", "zip": "zip", "render": "render"}


def test_tag_actions_empty_defaults() -> None:
    assert parse_tag_actions("") == DEFAULT_TAG_ACTIONS


def test_tag_actions_invalid_compact_raises() -> None:
    with pytest.raises(ConfigError):
        parse_tag_actions("extract")


def test_tag_actions_invalid_json_raises() -> None:
    with pytest.raises(ConfigError):
        parse_tag_actions("{not json}")


def test_settings_parses_tag_actions_field() -> None:
    s = Settings(
        NEXTCLOUD_URL="https://x",
        NC_USER="svc",
        NC_APP_PASSWORD="pw",
        TAG_ACTIONS="a:extract,b:zip",
    )
    assert s.TAG_ACTIONS == {"a": "extract", "b": "zip"}

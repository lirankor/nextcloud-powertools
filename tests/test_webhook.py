"""Webhook server tests (FastAPI TestClient; pipeline.process is stubbed).

Covers M3 DEMO.md webhook bullets: correct secret -> 200 + dispatch; wrong /
missing secret -> 401 (constant-time compare); both payload shapes parse;
unassign / unknown ignored (200 no-op); GET /healthz -> 200.
"""

from __future__ import annotations

import threading
from pathlib import Path

from fastapi.testclient import TestClient

from ncpowertools.config import Settings
from ncpowertools.models import TagEvent
from ncpowertools.webhook import create_app, parse_event

BASE = "https://cloud.example.com"
USER = "powertools"


class _SpyPipeline:
    """Records dispatched events; signals when one arrives (executor is async)."""

    def __init__(self) -> None:
        self.events: list[TagEvent] = []
        self.got = threading.Event()

    def process(self, event: TagEvent) -> None:
        self.events.append(event)
        self.got.set()


def _settings(tmp_path: Path, **over: object) -> Settings:
    kw: dict[str, object] = {
        "NEXTCLOUD_URL": BASE,
        "NC_USER": USER,
        "NC_APP_PASSWORD": "app-pw",
        "WEBHOOK_SECRET": "s3cret",
        "WORK_DIR": str(tmp_path),
    }
    kw.update(over)
    return Settings(**kw)  # type: ignore[arg-type]


TAG_ASSIGNED = {
    "event": {
        "class": "OCP\\SystemTag\\TagAssignedEvent",
        "objectType": "files",
        "objectIds": ["75"],
        "tagIds": [1],
    },
    "user": {"uid": "alice", "displayName": "Alice"},
    "time": 1700100000,
}

MAPPER_ASSIGN = {
    "event": {
        "class": "OCP\\SystemTag\\MapperEvent",
        "eventType": "OCP\\SystemTag\\ISystemTagObjectMapper::assignTags",
        "objectId": "88",
        "tagIds": [2],
    },
    "user": {"uid": "bob"},
}

MAPPER_UNASSIGN = {
    "event": {
        "class": "OCP\\SystemTag\\MapperEvent",
        "eventType": "OCP\\SystemTag\\ISystemTagObjectMapper::unassignTags",
        "objectId": "88",
        "tagIds": [2],
    },
    "user": {"uid": "bob"},
}

UNASSIGNED = {
    "event": {"class": "OCP\\SystemTag\\TagUnassignedEvent", "objectIds": ["75"], "tagIds": [1]},
    "user": {"uid": "alice"},
}


# --------------------------------------------------------------------------- #
# parse_event (pure)
# --------------------------------------------------------------------------- #


def test_parse_tag_assigned() -> None:
    ev = parse_event(TAG_ASSIGNED)
    assert ev is not None
    assert ev.uid == "alice" and ev.fileids == [75] and ev.tagids == [1]


def test_parse_mapper_assign() -> None:
    ev = parse_event(MAPPER_ASSIGN)
    assert ev is not None
    assert ev.uid == "bob" and ev.fileids == [88]


def test_parse_mapper_unassign_ignored() -> None:
    assert parse_event(MAPPER_UNASSIGN) is None


def test_parse_unassigned_ignored() -> None:
    assert parse_event(UNASSIGNED) is None


def test_parse_unknown_class_ignored() -> None:
    assert parse_event({"event": {"class": "OCP\\Files\\SomethingElse"}}) is None


# --------------------------------------------------------------------------- #
# HTTP behavior
# --------------------------------------------------------------------------- #


def _client(tmp_path: Path, **over: object) -> tuple[TestClient, _SpyPipeline, Settings]:
    settings = _settings(tmp_path, **over)
    spy = _SpyPipeline()
    app = create_app(spy, settings)  # type: ignore[arg-type]
    return TestClient(app), spy, settings


def test_healthz(tmp_path: Path) -> None:
    client, _, _ = _client(tmp_path)
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


def test_correct_secret_dispatches(tmp_path: Path) -> None:
    client, spy, settings = _client(tmp_path)
    r = client.post(
        settings.WEBHOOK_PATH,
        json=TAG_ASSIGNED,
        headers={"Authorization": "Bearer s3cret"},
    )
    assert r.status_code == 200
    assert spy.got.wait(2.0)
    assert spy.events[0].fileids == [75]


def test_wrong_secret_401_no_dispatch(tmp_path: Path) -> None:
    client, spy, settings = _client(tmp_path)
    r = client.post(
        settings.WEBHOOK_PATH, json=TAG_ASSIGNED, headers={"Authorization": "Bearer nope"}
    )
    assert r.status_code == 401
    assert not spy.got.wait(0.2)


def test_missing_secret_401(tmp_path: Path) -> None:
    client, _, settings = _client(tmp_path)
    r = client.post(settings.WEBHOOK_PATH, json=TAG_ASSIGNED)
    assert r.status_code == 401


def test_raw_header_secret(tmp_path: Path) -> None:
    client, spy, settings = _client(
        tmp_path, WEBHOOK_HEADER="X-Webhook-Secret"
    )
    r = client.post(
        settings.WEBHOOK_PATH, json=MAPPER_ASSIGN, headers={"X-Webhook-Secret": "s3cret"}
    )
    assert r.status_code == 200
    assert spy.got.wait(2.0)
    assert spy.events[0].fileids == [88]


def test_unassign_payload_200_noop(tmp_path: Path) -> None:
    client, spy, settings = _client(tmp_path)
    r = client.post(
        settings.WEBHOOK_PATH, json=UNASSIGNED, headers={"Authorization": "Bearer s3cret"}
    )
    assert r.status_code == 200
    assert not spy.got.wait(0.2)


def test_bad_json_400(tmp_path: Path) -> None:
    client, _, settings = _client(tmp_path)
    r = client.post(
        settings.WEBHOOK_PATH,
        content=b"not json",
        headers={"Authorization": "Bearer s3cret", "Content-Type": "application/json"},
    )
    assert r.status_code == 400

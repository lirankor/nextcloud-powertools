"""FastAPI webhook server for Nextcloud ``webhook_listeners`` deliveries.

NC posts a JSON envelope when a tag-assignment event fires (NC32+, from a
~5-min background cron — see CONTEXT.md §5). There is **no HMAC/signature**: the
only authentication is a static shared-secret header we registered. So the
single trust check here is a constant-time compare of that header against
``WEBHOOK_SECRET`` (``hmac.compare_digest``); TLS (terminated at the reverse
proxy) protects the secret in flight. Missing/mismatched -> 401.

We accept both payload shapes:

* ``TagAssignedEvent`` — ``event.objectIds`` (plural) + ``event.tagIds`` +
  ``user.uid``.
* legacy ``MapperEvent`` — ``event.objectId`` (singular) + an ``event.eventType``
  discriminator; we act only when it ends with ``assignTags`` and ignore the
  unassign variant.

Anything else (unassign, unknown class) is a 200 no-op. The actual work is
dispatched to a background executor so we return 200 fast (NC doesn't retry, and
the handler can take a while).
"""

from __future__ import annotations

import hmac
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, Request, Response

from .logging import get_logger
from .models import TagEvent

if TYPE_CHECKING:
    from .config import Settings
    from .pipeline import Pipeline

log = get_logger("webhook")

_TAG_ASSIGNED = "TagAssignedEvent"
_MAPPER = "MapperEvent"


def parse_event(payload: dict[str, Any]) -> TagEvent | None:
    """Normalize an NC envelope into a :class:`TagEvent`, or ``None`` to ignore.

    Returns ``None`` for unassign events and any class we don't act on.
    """
    event = payload.get("event")
    if not isinstance(event, dict):
        return None
    cls = str(event.get("class", ""))
    user = payload.get("user")
    uid = str(user.get("uid", "")) if isinstance(user, dict) else ""

    tagids = _as_int_list(event.get("tagIds"))

    if cls.endswith(_TAG_ASSIGNED):
        fileids = _as_int_list(event.get("objectIds"))
        if not fileids:
            return None
        return TagEvent(uid=uid, fileids=fileids, tagids=tagids, raw=payload)

    if cls.endswith(_MAPPER):
        # Only the assign variant; eventType is e.g. "assignTags"/"unassignTags".
        event_type = str(event.get("eventType", ""))
        if not event_type.endswith("assignTags") or event_type.endswith("unassignTags"):
            return None
        obj = event.get("objectId")
        if obj is None:
            return None
        return TagEvent(uid=uid, fileids=_as_int_list([obj]), tagids=tagids, raw=payload)

    return None


def _as_int_list(value: object) -> list[int]:
    if not isinstance(value, list):
        return []
    out: list[int] = []
    for v in value:
        try:
            out.append(int(v))
        except (TypeError, ValueError):
            continue
    return out


def create_app(pipeline: Pipeline, settings: Settings) -> FastAPI:
    """Build the FastAPI app. Work is dispatched on a small thread pool."""
    executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ncpt-hook")

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            executor.shutdown(wait=True)

    app = FastAPI(
        title="nextcloud-powertools", docs_url=None, redoc_url=None, lifespan=lifespan
    )
    app.state.executor = executor

    expected = settings.WEBHOOK_SECRET
    header_name = settings.WEBHOOK_HEADER
    is_authorization = header_name.lower() == "authorization"

    def _secret_ok(request: Request) -> bool:
        if not expected:
            return False
        presented = request.headers.get(header_name, "")
        if is_authorization:
            scheme, _, token = presented.partition(" ")
            if scheme.lower() != "bearer":
                # Compare anyway (constant-time) against empty to avoid leaking
                # via early return timing.
                return hmac.compare_digest(presented, expected) and False
            presented = token
        return hmac.compare_digest(presented, expected)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post(settings.WEBHOOK_PATH)
    async def hook(request: Request) -> Response:
        if not _secret_ok(request):
            log.warning("webhook auth rejected")
            return Response(status_code=401)
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001 - malformed body
            return Response(status_code=400)
        if not isinstance(payload, dict):
            return Response(status_code=400)

        event = parse_event(payload)
        if event is None:
            log.info("webhook ignored (unhandled event)")
            return Response(status_code=200)

        log.info(
            "webhook accepted",
            extra={"uid": event.uid, "fileids": event.fileids, "tagids": event.tagids},
        )
        executor.submit(pipeline.process, event)
        return Response(status_code=200)

    return app

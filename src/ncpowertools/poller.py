"""Polling loop — the primary, universal trigger path (works NC30+).

Webhooks require NC32+ and fire from a ~5-min cron, so polling is the baseline
(CONTEXT.md §5, PLAN.md). Each sweep, for every configured trigger tag, we
systemtag-search the target user's namespace and synthesize a
:class:`~ncpowertools.models.TagEvent` per matching file, then run it through
the same :class:`~ncpowertools.pipeline.Pipeline`. Idempotency is handled by the
pipeline (per-fileid lock + remove-tag-on-success + failure backoff), so a file
that's already been processed simply won't carry the trigger tag next sweep.

``POLL_INTERVAL == 0`` disables polling (webhook-only deployments).
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from .errors import NcApiError
from .logging import get_logger
from .models import TagEvent

if TYPE_CHECKING:
    from .config import Settings
    from .nextcloud import NextcloudClient
    from .pipeline import Pipeline

log = get_logger("poller")


class Poller:
    """Repeatedly searches for tagged files and feeds them to the pipeline."""

    def __init__(self, client: NextcloudClient, pipeline: Pipeline, settings: Settings) -> None:
        self.client = client
        self.pipeline = pipeline
        self.settings = settings
        self._stop = threading.Event()

    def sweep(self) -> int:
        """Run one pass over all configured trigger tags. Returns files seen."""
        seen = 0
        for tag_name in self.settings.TAG_ACTIONS:
            try:
                tag = self.client.ensure_tag(tag_name)
            except NcApiError as exc:
                log.warning("poll: could not resolve tag", extra={"tag": tag_name, "err": str(exc)})
                continue
            if tag.id is None:
                continue
            try:
                refs = self.client.search_by_tag(tag.id, user=self.settings.TARGET_USER)
            except NcApiError as exc:
                log.warning("poll: search failed", extra={"tag": tag_name, "err": str(exc)})
                continue
            for ref in refs:
                seen += 1
                # Carry the FULL FileRef (with path + is_dir) that search_by_tag
                # already resolved via the supported oc:systemtag filter, so the
                # pipeline uses it directly and never re-resolves by fileid (the
                # oc:fileid filter-rule NC ignores — the LIVE bug, M7).
                event = TagEvent(
                    uid=self.settings.TARGET_USER,
                    fileids=[ref.fileid],
                    tagids=[tag.id],
                    files=[ref],
                    raw={"source": "poller", "tag": tag_name},
                )
                self.pipeline.process(event)
        log.info("poll sweep done", extra={"files": seen})
        return seen

    def run_forever(self) -> None:
        """Sweep every ``POLL_INTERVAL`` seconds until :meth:`stop` is called."""
        interval = self.settings.POLL_INTERVAL
        if interval <= 0:
            log.info("poller disabled (POLL_INTERVAL=0)")
            return
        log.info("poller starting", extra={"interval": interval})
        while not self._stop.is_set():
            try:
                self.sweep()
            except Exception:  # noqa: BLE001 - a bad sweep must not kill the loop
                log.exception("poll sweep error")
            self._stop.wait(interval)
        log.info("poller stopped")

    def stop(self) -> None:
        self._stop.set()

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

from .config import immich_album_from_tag, is_immich_tag
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
        # Fixed-tag loop: every statically-mapped TAG_ACTIONS entry.
        seen_tagids: set[int] = set()
        for tag_name in self.settings.TAG_ACTIONS:
            try:
                tag = self.client.ensure_tag(tag_name)
            except NcApiError as exc:
                log.warning("poll: could not resolve tag", extra={"tag": tag_name, "err": str(exc)})
                continue
            if tag.id is None:
                continue
            seen_tagids.add(tag.id)
            seen += self._sweep_tag(tag.id, tag_name, album=None)

        # Immich (F6): parameterized/prefix trigger tags can't live in the static
        # TAG_ACTIONS map, so when enabled we list ALL system tags and pick those
        # named `<IMMICH_TAG>` (exact) or `<IMMICH_TAG>-<album>` (prefix). The
        # album is parsed from the suffix and carried on the event. We dedupe
        # against tag ids already swept above so a stray immich entry in
        # TAG_ACTIONS doesn't double-process.
        if self.settings.ENABLE_IMMICH:
            seen += self._sweep_immich(seen_tagids)

        log.info("poll sweep done", extra={"files": seen})
        return seen

    def _sweep_tag(self, tagid: int, tag_name: str, album: str | None) -> int:
        """Search one tag id and feed each matching file into the pipeline."""
        try:
            refs = self.client.search_by_tag(tagid, user=self.settings.TARGET_USER)
        except NcApiError as exc:
            log.warning("poll: search failed", extra={"tag": tag_name, "err": str(exc)})
            return 0
        count = 0
        for ref in refs:
            count += 1
            raw: dict[str, object] = {"source": "poller", "tag": tag_name}
            if album is not None:
                raw["immich_album"] = album
            # Carry the FULL FileRef (with path + is_dir) that search_by_tag
            # already resolved via the supported oc:systemtag filter, so the
            # pipeline uses it directly and never re-resolves by fileid (the
            # oc:fileid filter-rule NC ignores — the LIVE bug, M7).
            event = TagEvent(
                uid=self.settings.TARGET_USER,
                fileids=[ref.fileid],
                tagids=[tagid],
                files=[ref],
                raw=raw,
            )
            self.pipeline.process(event)
        return count

    def _sweep_immich(self, skip_tagids: set[int]) -> int:
        """List system tags + sweep every immich / immich-<album> trigger tag."""
        try:
            tags = self.client.list_tags()
        except NcApiError as exc:
            log.warning("poll: could not list tags for immich", extra={"err": str(exc)})
            return 0
        count = 0
        for tag in tags:
            if tag.id is None or tag.id in skip_tagids:
                continue
            if not is_immich_tag(tag.name, self.settings.IMMICH_TAG):
                continue
            skip_tagids.add(tag.id)
            album = immich_album_from_tag(tag.name, self.settings.IMMICH_TAG)
            count += self._sweep_tag(tag.id, tag.name, album=album)
        return count

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

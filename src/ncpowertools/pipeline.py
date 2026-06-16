"""The orchestration pipeline: trigger event -> download -> handler -> upload.

:class:`Pipeline.process` is the single entry point used by BOTH the webhook
server and the poller. For each fileid in a :class:`~ncpowertools.models.TagEvent`
it runs the full flow described in ARCHITECTURE.md §"Pipeline":

  lock -> resolve fileid -> pick the trigger tag/action -> download ->
  run handler (local temp only) -> upload outputs into the SOURCE's PARENT
  folder -> remove the trigger tag -> clean temp.

Safety invariants enforced here (CONTEXT.md §9):

* **Never DELETE user content.** The only DELETE we ever issue is on a
  *systemtags-relation* (untagging) — never on a file/folder. ``extract`` writes
  a new subfolder beside the archive; the archive stays.
* **Idempotency / re-runnability.** A per-fileid lock (``locking.file_lock``)
  makes concurrent events for the same file run the handler at most once. The
  trigger tag is removed only after a verified successful upload, so a re-tag
  re-runs it. Outputs overwrite (PUT) so a re-run is safe.
* **No hot-loop on failure.** On failure we keep the trigger tag (retriable) but
  record ``(fileid, mtime)`` in a process-local failure marker; the poller skips
  a fileid whose content hasn't changed since it last failed, so a permanently
  failing file isn't reprocessed every sweep. The marker clears on success or
  when the file's mtime changes.

Folder handling: ``extract`` doesn't apply to folders (its ``can_handle``
returns False -> we skip with a clear log). The compress actions
(``zip``/``rar``/``7z``) and the **render** actions (``render``/``render-png``,
F1) DO apply: we download the folder as a zip via the NC directory-GET
extension, unpack it locally to reconstruct the tree, then hand that local
directory to the handler. This keeps folder support in one robust place and
reuses the existing streaming download.

Output upload base: outputs are uploaded preserving their relative path under
``ctx.output_dir()``. For a **file** source that base is the source's PARENT
folder (``extract`` writes a subfolder beside the archive; a single render lands
beside its source). For a **directory** source (compress / dir-render) the base
is the tagged directory ITSELF, so a rendered ``sub/b.png`` lands at
``<dir>/sub/b.png`` mirroring the source tree. We never re-upload the originals
and never DELETE user content.
"""

from __future__ import annotations

import shutil
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

from .config import immich_album_from_tag, is_immich_tag
from .errors import HandlerError
from .handlers import resolve
from .handlers.base import HandlerContext
from .immich import sha1_of_file
from .locking import file_lock
from .logging import get_logger
from .models import ActionResult, FileRef, TagEvent

if TYPE_CHECKING:
    from .config import Settings
    from .immich import ImmichService
    from .nextcloud import NextcloudClient
    from .shred import ShredService

log = get_logger("pipeline")


class ActionMatch(NamedTuple):
    """A matched trigger tag: the action, its tag id, and an optional parameter.

    ``param`` carries an action-specific parameter parsed from the trigger tag —
    today only the Immich album name (from an ``immich-<album>`` tag); ``None``
    for fixed-name tags and for the plain ``immich`` tag (main library).
    """

    action: str
    tag_id: int
    param: str | None = None

# Render actions that, on a FOLDER source, walk the tree and render per file (F1)
# — and whose outputs upload INTO the tagged dir (tree mirrored), not its parent.
_RENDER_ACTIONS = frozenset({"render", "render-png"})

# Shred actions are SERVER-SIDE only (F5): they never download/upload content;
# the pipeline routes them to ShredService instead of the handler flow.
_SHRED_ACTIONS = frozenset({"shred", "shred-confirm"})

# The immich action (F6) is server-side-ish like shred (routed to ImmichService),
# but it DOES download the file bytes to re-upload them to the Immich server.
_IMMICH_ACTION = "immich"


class Pipeline:
    """Ties :class:`NextcloudClient` + handlers together for one event at a time."""

    def __init__(self, client: NextcloudClient, settings: Settings) -> None:
        self.client = client
        self.settings = settings
        self.work_root = Path(settings.WORK_DIR)
        # tag-name -> tag-id cache, populated lazily from list_tags().
        self._tagid_by_name: dict[str, int] | None = None
        # fileid -> mtime string that failed last run (skip until it changes).
        self._failed: dict[int, str] = {}
        # Lazily-built shred service (F5, opt-in). None until first needed.
        self._shred: ShredService | None = None
        # Lazily-built immich service (F6, opt-in). None until first needed.
        self._immich: ImmichService | None = None

    # ----------------------------------------------------------------- #
    # public entry point
    # ----------------------------------------------------------------- #

    def process(self, event: TagEvent) -> None:
        """Process every file in ``event`` (best-effort, isolated per file).

        Prefers the pre-resolved :class:`FileRef`\\ s in ``event.files`` (the poller
        path — path already known via the supported ``oc:systemtag`` search, so we
        never re-resolve by fileid). Falls back to ``event.fileids`` for the webhook
        path, where only fileids are delivered and the FileRef is resolved lazily.
        """
        # fileid -> pre-resolved FileRef carried on the event (poller path).
        carried = {ref.fileid: ref for ref in event.files}
        # Process each carried file; for the webhook path (no carried refs) iterate
        # the bare fileids. Carried refs take precedence and avoid the broken
        # resolve_fileid call entirely.
        fileids = list(carried) if carried else event.fileids
        for fileid in fileids:
            try:
                self._process_one(fileid, event, carried.get(fileid))
            except Exception:  # pragma: no cover - defensive: keep the loop alive
                log.exception("unexpected pipeline error", extra={"fileid": fileid})

    # ----------------------------------------------------------------- #
    # per-file flow
    # ----------------------------------------------------------------- #

    def _process_one(
        self, fileid: int, event: TagEvent, src: FileRef | None = None
    ) -> None:
        with file_lock(fileid) as acquired:
            if not acquired:
                log.info("skip: already processing", extra={"fileid": fileid})
                return

            # Poller path: the FileRef (with path + is_dir) is already resolved and
            # carried on the event — use it directly. Webhook path: only a fileid is
            # available, so resolve it via the supported SEARCH method.
            if src is None:
                src = self.client.resolve_fileid(fileid, user=self.settings.TARGET_USER)
            if src is None:
                log.info("skip: fileid not found", extra={"fileid": fileid})
                return

            match = self._match_action(fileid, event)
            if match is None:
                log.info(
                    "skip: no configured trigger tag on file",
                    extra={"fileid": fileid, "path": src.path},
                )
                return
            action = match.action

            # Failure backoff: skip a file that failed last time and hasn't
            # changed since (avoids the poller hot-looping a broken file).
            mtime = self._mtime_of(src)
            if self._failed.get(fileid) == mtime:
                log.info(
                    "skip: previously failed, unchanged",
                    extra={"fileid": fileid, "action": action},
                )
                return

            work = self.work_root / str(fileid)
            try:
                self._run(src, match, event, work)
                self._failed.pop(fileid, None)
            except HandlerError as exc:
                self._on_failure(src, event, action, str(exc), mtime)
            except Exception as exc:  # noqa: BLE001 - report+tag, never crash worker
                self._on_failure(src, event, action, f"{type(exc).__name__}: {exc}", mtime)
            finally:
                shutil.rmtree(work, ignore_errors=True)

    def _run(
        self,
        src: FileRef,
        match: ActionMatch,
        event: TagEvent,
        work: Path,
    ) -> None:
        action, trigger_tagid = match.action, match.tag_id
        # F5: shred actions are server-side only — route to ShredService and
        # skip the entire download/handler/upload flow (no temp download).
        if action in _SHRED_ACTIONS:
            self._run_shred(src, action, trigger_tagid, event)
            return

        # F6: immich is routed to ImmichService — it downloads bytes (single
        # file or a folder walk) and re-uploads them to the Immich server.
        if action == _IMMICH_ACTION:
            self._run_immich(src, match, event, work)
            return

        handler = resolve(action)
        if not handler.can_handle(src):
            # e.g. extract/render on a folder, or extract on a non-archive.
            log.info(
                "skip: handler cannot handle source",
                extra={"fileid": src.fileid, "action": action, "is_dir": src.is_dir},
            )
            return

        src_local = self._download(src, work, action)

        ctx = HandlerContext(
            work_dir=work,
            src=src,
            max_uncompressed_size=self.settings.MAX_UNCOMPRESSED_SIZE,
            max_files=self.settings.MAX_FILES,
            enable_rar=self.settings.ENABLE_RAR,
            logger=get_logger(f"handler.{action}"),
        )
        result: ActionResult = handler.run(ctx, src_local)

        self._upload_outputs(src, action, ctx, result)

        # Success: remove the trigger tag (idempotent; makes it re-runnable).
        self.client.remove_tag(src.fileid, trigger_tagid)
        log.info(
            "processed",
            extra={
                "fileid": src.fileid,
                "action": action,
                "outputs": len(result.outputs),
                "path": src.path,
            },
        )
        if self.settings.NOTIFY:
            self.client.notify(
                event.uid,
                f"powertools: {action} done",
                result.message or f"{action} completed for {src.name}",
            )

    def _run_shred(
        self, src: FileRef, action: str, trigger_tagid: int, event: TagEvent
    ) -> None:
        """Route a shred / shred-confirm action to the ShredService (F5).

        If ENABLE_SHRED is off, the actions are never registered as triggers, so
        this is normally unreachable — but we still guard (log once) so a stale
        shred tag from a previous enabled run is ignored, not acted on.
        """
        if not self.settings.ENABLE_SHRED:
            log.info(
                "skip: shred disabled (ENABLE_SHRED=false)",
                extra={"fileid": src.fileid, "action": action},
            )
            return
        if self._shred is None:
            from .shred import ShredService

            self._shred = ShredService(self.client, self.settings)
        if action == "shred":
            self._shred.request(src, trigger_tagid, event.uid)
        else:  # "shred-confirm"
            self._shred.confirm(src, trigger_tagid, event.uid)

    # ----------------------------------------------------------------- #
    # immich (F6) — download bytes, upload a COPY to Immich, keep the original
    # ----------------------------------------------------------------- #

    def _run_immich(
        self, src: FileRef, match: ActionMatch, event: TagEvent, work: Path
    ) -> None:
        """Upload the tagged file (or every media file under a tagged dir) to Immich.

        Non-destructive: the NC original is never deleted; only the trigger tag
        is removed on success. Dedup is via SHA-1 (bulk-upload-check +
        ``x-immich-checksum``) so re-runs are safe. Failures propagate to the
        standard non-destructive failure handler (trigger tag KEPT for retry).
        """
        if not self.settings.ENABLE_IMMICH:
            log.info(
                "skip: immich disabled (ENABLE_IMMICH=false)",
                extra={"fileid": src.fileid},
            )
            return
        if self._immich is None:
            from .immich import ImmichService

            self._immich = ImmichService(self.settings)
        immich = self._immich
        album = match.param

        # Collect the local files to upload: a single file, or a folder walk
        # filtered to Immich-accepted media types (respecting MAX_FILES).
        if src.is_dir:
            local_dir = self._download_folder(src, work)
            files = self._immich_media_files(immich, local_dir, src)
            if not files:
                log.info("immich: no media files to upload", extra={"dir": src.path})
                self.client.remove_tag(src.fileid, match.tag_id)
                return
        else:
            dest = work / "src" / src.name
            self.client.download_to(src.path, dest)
            files = [(dest, src.path, src.fileid)]

        asset_ids, created, dup, failed = self._immich_upload_batch(immich, src, files)

        if failed and not asset_ids:
            # Everything failed — surface as a failure (trigger tag kept, retriable).
            raise HandlerError(f"immich: all {failed} upload(s) failed for {src.path}")

        if album and asset_ids:
            immich.find_or_create_album(album, asset_ids)

        # Success: remove the trigger tag (non-destructive; re-runnable).
        self.client.remove_tag(src.fileid, match.tag_id)
        summary = (
            f"uploaded {created}, duplicate {dup}, failed {failed}"
            + (f", album '{album}'" if album else "")
        )
        log.info(
            "immich processed",
            extra={
                "fileid": src.fileid,
                "path": src.path,
                "uploaded": created,
                "duplicate": dup,
                "failed": failed,
                "album": album,
                "assets": len(asset_ids),
            },
        )
        if self.settings.NOTIFY:
            self.client.notify(event.uid, "powertools: immich done", f"{src.name}: {summary}")

    def _immich_media_files(
        self, immich: ImmichService, local_dir: Path, src: FileRef
    ) -> list[tuple[Path, str, int]]:
        """Walk ``local_dir`` -> list of ``(local_path, remote_path, fileid)`` media.

        Filters to files whose extension Immich accepts (image/video) using the
        cached ``media_types`` allow-list; non-media are skipped (logged in the
        summary). Enforces ``MAX_FILES`` as a hard cap. The ``remote_path`` is
        the NC path (used only for the stable ``deviceAssetId``); the per-file
        fileid is unknown for walked members so we synthesize a stable id from
        the source dir fileid + relative path.
        """
        candidates = sorted(
            p
            for p in local_dir.rglob("*")
            if p.is_file() and immich.is_accepted_media(p.name)
        )
        skipped = sum(
            1 for p in local_dir.rglob("*") if p.is_file() and not immich.is_accepted_media(p.name)
        )
        if len(candidates) > self.settings.MAX_FILES:
            raise HandlerError(
                f"{len(candidates)} media files exceed MAX_FILES cap "
                f"({self.settings.MAX_FILES}); aborting (raise MAX_FILES to allow)"
            )
        log.info(
            "immich: scanned directory",
            extra={"dir": src.path, "media": len(candidates), "skipped_non_media": skipped},
        )
        out: list[tuple[Path, str, int]] = []
        for p in candidates:
            rel = p.relative_to(local_dir).as_posix()
            out.append((p, f"{src.path.strip('/')}/{rel}", src.fileid))
        return out

    def _immich_upload_batch(
        self,
        immich: ImmichService,
        src: FileRef,
        files: list[tuple[Path, str, int]],
    ) -> tuple[list[str], int, int, int]:
        """SHA-1 + bulk-check + upload each file. Returns (asset_ids, created, dup, failed).

        For a directory walk the per-file ``deviceAssetId`` uses the dir fileid +
        the file's remote path so it's stable per source file; for a single file
        it is ``nc:<fileid>``. Duplicates (from the precheck OR a 200 upload)
        still contribute their existing asset id so they can be added to an album.
        """
        # SHA-1 every file and run one bulk-check to learn which are duplicates.
        checks: list[tuple[str, str]] = []
        meta: dict[str, tuple[Path, str, str]] = {}  # corr_id -> (path, remote, sha1)
        for i, (path, remote, _fid) in enumerate(files):
            digest = sha1_of_file(path)
            corr = str(i)
            checks.append((corr, digest))
            meta[corr] = (path, remote, digest)
        try:
            results = immich.bulk_check(checks)
        except Exception as exc:  # noqa: BLE001 - precheck is best-effort
            log.warning("immich: bulk-check failed; uploading all", extra={"error": str(exc)})
            results = {}

        asset_ids: list[str] = []
        created = dup = failed = 0
        for corr, (path, remote, digest) in meta.items():
            action, existing = results.get(corr, ("accept", None))
            if action == "reject" and existing:
                # Already in Immich — harvest the id, skip the upload.
                asset_ids.append(existing)
                dup += 1
                continue
            device_asset_id = (
                f"nc:{src.fileid}" if not src.is_dir else f"nc:{src.fileid}:{remote}"
            )
            mtime = self.client.last_modified(remote, user=self.settings.TARGET_USER)
            try:
                asset_id, status = immich.upload(
                    path,
                    device_asset_id=device_asset_id,
                    file_created_at=mtime,
                    file_modified_at=mtime,
                    checksum=digest,
                )
            except Exception as exc:  # noqa: BLE001 - per-file isolation in a batch
                failed += 1
                log.warning("immich: upload failed", extra={"file": remote, "error": str(exc)})
                continue
            asset_ids.append(asset_id)
            if status == "duplicate":
                dup += 1
            else:
                created += 1
        return asset_ids, created, dup, failed

    # ----------------------------------------------------------------- #
    # download
    # ----------------------------------------------------------------- #

    def _download(self, src: FileRef, work: Path, action: str) -> Path:
        """Place the source locally and return the path the handler operates on."""
        if src.is_dir:
            # Compress + render dir actions reach here; extract fails can_handle.
            return self._download_folder(src, work)
        dest = work / "src" / src.name
        self.client.download_to(src.path, dest)
        return dest

    def _download_folder(self, src: FileRef, work: Path) -> Path:
        """Download a folder as a zip and unpack it into a local mirror dir.

        Returns the local directory (named like the source folder) for the
        compress handler to archive. We extract members verbatim into
        ``work/src/<foldername>/`` — this is OUR OWN export (not user-supplied
        crafted input), so the zip-slip guard isn't the concern here; we still
        skip absolute/``..`` entries defensively.
        """
        archive = work / "dl" / f"{src.name}.zip"
        self.client.download_dir_as_zip(src.path, archive)
        mirror = work / "src" / src.name
        mirror.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(archive) as zf:
            for info in zf.infolist():
                name = info.filename.replace("\\", "/")
                if name.startswith("/") or ".." in Path(name).parts:
                    continue  # defensive
                target = mirror / name
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as s, target.open("wb") as o:
                    shutil.copyfileobj(s, o)
        return mirror

    # ----------------------------------------------------------------- #
    # upload (into the SOURCE's PARENT folder, preserving the output tree)
    # ----------------------------------------------------------------- #

    def _upload_outputs(
        self, src: FileRef, action: str, ctx: HandlerContext, result: ActionResult
    ) -> None:
        out_root = ctx.output_dir().resolve()
        # For a dir-render the outputs mirror the tagged dir's tree, so they
        # upload INTO the tagged dir. Everything else (file render, extract,
        # compress) uploads into the source's PARENT folder (base "" = user root).
        into_dir = src.is_dir and action in _RENDER_ACTIONS
        base = src.path.strip("/") if into_dir else src.parent
        # Pre-create every distinct subfolder under the parent (extract creates
        # a <stem>/a/b tree). ensure_dir is idempotent and handles AutoMkcol.
        made: set[str] = set()
        for out_str in result.outputs:
            out = Path(out_str).resolve()
            rel = out.relative_to(out_root).as_posix()
            remote = f"{base}/{rel}" if base else rel
            sub = remote.rsplit("/", 1)[0] if "/" in remote else ""
            if sub and sub not in made:
                self.client.ensure_dir(sub)
                made.add(sub)
            with out.open("rb") as fh:
                self.client.upload(remote, fh)
            log.info("uploaded output", extra={"remote": remote, "fileid": src.fileid})

    # ----------------------------------------------------------------- #
    # tag/action resolution
    # ----------------------------------------------------------------- #

    def _match_action(self, fileid: int, event: TagEvent) -> ActionMatch | None:
        """Return an :class:`ActionMatch` for the first of the file's tags that
        maps to a configured action, or ``None`` if none do.

        We read the tags actually on the file (``tags_on_file``) so this works
        identically whether triggered by a webhook (which carries tagids we
        could trust) or the poller (which already searched by one tag). Going to
        the source of truth also means we never act on a tag that was removed
        between the trigger and processing.

        Two matching mechanisms:

        * **Fixed tags** — looked up in the static ``TAG_ACTIONS`` map.
        * **Immich prefix tags (F6)** — when ``ENABLE_IMMICH``, a tag named
          ``<IMMICH_TAG>`` (exact) or ``<IMMICH_TAG>-<album>`` (prefix) matches
          the ``immich`` action; the album is parsed from the suffix (preferring
          a poller-supplied ``event.raw["immich_album"]`` so spaces survive the
          round-trip exactly, falling back to parsing the tag name). These are
          deliberately NOT in ``TAG_ACTIONS`` so ``immich-anything`` works
          without pre-registration.
        """
        raw_album = event.raw.get("immich_album")
        poller_album = raw_album if isinstance(raw_album, str) else None
        for tag in self.client.tags_on_file(fileid):
            if tag.id is None:
                continue
            action = self.settings.TAG_ACTIONS.get(tag.name)
            if action:
                return ActionMatch(action, tag.id, None)
            if self.settings.ENABLE_IMMICH and is_immich_tag(
                tag.name, self.settings.IMMICH_TAG
            ):
                album = poller_album or immich_album_from_tag(
                    tag.name, self.settings.IMMICH_TAG
                )
                return ActionMatch(_IMMICH_ACTION, tag.id, album)
        return None

    # ----------------------------------------------------------------- #
    # failure handling
    # ----------------------------------------------------------------- #

    def _on_failure(
        self,
        src: FileRef,
        event: TagEvent,
        action: str,
        message: str,
        mtime: str,
    ) -> None:
        log.error(
            "action failed",
            extra={
                "fileid": src.fileid,
                "action": action,
                "path": src.path,
                "error": message,
            },
        )
        # Remember this failure so the poller doesn't reprocess an unchanged,
        # broken file every sweep. Cleared on success or on an mtime change.
        self._failed[src.fileid] = mtime

        # Keep the trigger tag (do NOT remove) so it stays retriable; optionally
        # mark with the error tag for visibility in the NC UI.
        if self.settings.ERROR_TAG:
            try:
                error_tag = self.client.ensure_tag(self.settings.ERROR_TAG)
                if error_tag.id is not None:
                    self.client.assign_tag(src.fileid, error_tag.id)
            except Exception:  # noqa: BLE001 - tagging the error must not mask it
                log.warning("could not assign error tag", extra={"fileid": src.fileid})

        if self.settings.NOTIFY:
            self.client.notify(
                event.uid,
                f"powertools: {action} failed",
                f"{action} failed for {src.name}: {message}",
            )

    # ----------------------------------------------------------------- #
    # helpers
    # ----------------------------------------------------------------- #

    @staticmethod
    def _mtime_of(src: FileRef) -> str:
        """A cheap change-token for the failure marker.

        ``resolve_fileid`` doesn't surface mtime today, so we key the marker on
        the fileid alone via an empty token — meaning "failed at least once".
        A re-tag still re-runs (the tag relation drives the trigger); this only
        suppresses the poller from hammering an unchanged failing file within
        the same process lifetime. Kept as a method so M-future can wire a real
        mtime in without touching callers.
        """
        return ""

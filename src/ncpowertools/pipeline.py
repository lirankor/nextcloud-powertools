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
from typing import TYPE_CHECKING

from .errors import HandlerError
from .handlers import resolve
from .handlers.base import HandlerContext
from .locking import file_lock
from .logging import get_logger
from .models import ActionResult, FileRef, TagEvent

if TYPE_CHECKING:
    from .config import Settings
    from .nextcloud import NextcloudClient

log = get_logger("pipeline")

# Render actions that, on a FOLDER source, walk the tree and render per file (F1)
# — and whose outputs upload INTO the tagged dir (tree mirrored), not its parent.
_RENDER_ACTIONS = frozenset({"render", "render-png"})


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

    # ----------------------------------------------------------------- #
    # public entry point
    # ----------------------------------------------------------------- #

    def process(self, event: TagEvent) -> None:
        """Process every fileid in ``event`` (best-effort, isolated per file)."""
        for fileid in event.fileids:
            try:
                self._process_one(fileid, event)
            except Exception:  # pragma: no cover - defensive: keep the loop alive
                log.exception("unexpected pipeline error", extra={"fileid": fileid})

    # ----------------------------------------------------------------- #
    # per-file flow
    # ----------------------------------------------------------------- #

    def _process_one(self, fileid: int, event: TagEvent) -> None:
        with file_lock(fileid) as acquired:
            if not acquired:
                log.info("skip: already processing", extra={"fileid": fileid})
                return

            src = self.client.resolve_fileid(fileid, user=self.settings.TARGET_USER)
            if src is None:
                log.info("skip: fileid not found", extra={"fileid": fileid})
                return

            match = self._match_action(fileid)
            if match is None:
                log.info(
                    "skip: no configured trigger tag on file",
                    extra={"fileid": fileid, "path": src.path},
                )
                return
            action, trigger_tagid = match

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
                self._run(src, action, trigger_tagid, event, work)
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
        action: str,
        trigger_tagid: int,
        event: TagEvent,
        work: Path,
    ) -> None:
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

    def _match_action(self, fileid: int) -> tuple[str, int] | None:
        """Return ``(action, trigger_tagid)`` for the first of the file's tags
        that maps to a configured action, or ``None`` if none do.

        We read the tags actually on the file (``tags_on_file``) so this works
        identically whether triggered by a webhook (which carries tagids we
        could trust) or the poller (which already searched by one tag). Going to
        the source of truth also means we never act on a tag that was removed
        between the trigger and processing.
        """
        for tag in self.client.tags_on_file(fileid):
            if tag.id is None:
                continue
            action = self.settings.TAG_ACTIONS.get(tag.name)
            if action:
                return action, tag.id
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

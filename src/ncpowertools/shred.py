"""``shred`` — the ONE deliberately destructive action (F5, opt-in).

This breaks the project's "never delete user content" invariant, so it is the
most heavily guarded thing in the codebase. It performs a **two-step handshake**
and a **permanent purge from Nextcloud** (WebDAV DELETE -> empty from trash; the
trash delete auto-purges that file's versions too). It is NOT secure erasure —
the storage layer, copy-on-write, object stores and especially the **Kopia /
hetzbox backups** still retain the data. We say "purge from Nextcloud", never
"securely shred".

We deliberately do **NOT** overwrite-before-delete: on Nextcloud that only
creates version bloat with zero security benefit (versions/trash/CoW/backups
persist regardless). So there is no overwrite PUT anywhere in this flow.

Flow (driven by the pipeline, which routes the two shred actions here instead of
the download/handler/upload path):

* **Step 1 — ``shred`` tag on a target** (:meth:`ShredService.request`):
  validate scope guards, compute size + file count, write a
  ``CONFIRM-SHRED-<fileid>-<safeName>.md`` receipt beside it (with
  machine-readable front-matter), then **remove the ``shred`` tag** from the
  target so state lives entirely in the receipt. No deletion happens here.

* **Step 2 — ``shred-confirm`` tag on a CONFIRM-SHRED receipt**
  (:meth:`ShredService.confirm`): parse the receipt's front-matter, re-resolve
  the target, **re-validate every guard**, confirm the fileid still matches,
  check capabilities, then DELETE the target -> find its trash node by fileid ->
  permanently delete that trash node. Rewrite the receipt as ``SHREDDED-…`` and
  remove the ``shred-confirm`` tag. Any failure keeps state and writes a FAILED
  note — it never reports success on a partial purge.

Scope is confined to ``SHRED_DIR`` within the service account's OWN namespace
(the app password can only address ``/files/<user>/`` anyway). Received shares
and external/group mounts are refused (a DELETE on those only unshares/unmounts
but returns success — it would falsely report a shred).
"""

from __future__ import annotations

import datetime
import re
from typing import TYPE_CHECKING

from .errors import NcApiError
from .logging import get_logger
from .models import FileRef

if TYPE_CHECKING:
    from .config import Settings
    from .nextcloud import NextcloudClient

log = get_logger("shred")

# Mount types that mean the data does NOT live in our own local namespace — a
# DELETE there only unmounts/unshares (owner keeps the data) yet returns success.
_NONLOCAL_MOUNTS = frozenset({"shared", "group", "external"})

_RECEIPT_PREFIX = "CONFIRM-SHRED-"
_SHREDDED_PREFIX = "SHREDDED-"
_FAILED_PREFIX = "FAILED-SHRED-"

# Front-matter keys parsed back out statelessly in step 2.
_FM_OPEN = "```ncpowertools-shred"
_FM_CLOSE = "```"


class ShredRefused(Exception):
    """A scope guard refused the operation (never a crash — caller logs)."""


def _safe_name(name: str) -> str:
    """A filesystem/url-safe slug of a file name for the receipt filename."""
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")
    return slug[:80] or "file"


def _now_iso() -> str:
    return datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ts_compact() -> str:
    return datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")


class ShredService:
    """Two-step guarded permanent-purge service (see module docstring)."""

    def __init__(self, client: NextcloudClient, settings: Settings) -> None:
        self.client = client
        self.settings = settings
        self.user = settings.TARGET_USER or settings.NC_USER

    # ----------------------------------------------------------------- #
    # public entry points (called by the pipeline)
    # ----------------------------------------------------------------- #

    def request(self, src: FileRef, trigger_tagid: int, uid: str) -> None:
        """Step 1: a ``shred`` tag was placed on ``src`` (a target to purge)."""
        if not self.settings.ENABLE_SHRED:
            log.info("shred disabled — ignoring shred tag", extra={"fileid": src.fileid})
            return
        try:
            self._guard_scope(src.path)
        except ShredRefused as exc:
            self._refuse(src, uid, str(exc), step="request")
            return

        try:
            props = self.client.propfind_props(src.path, user=self.user)
        except NcApiError as exc:
            self._refuse(src, uid, f"could not PROPFIND target: {exc}", step="request")
            return
        try:
            self._guard_props(props)
        except ShredRefused as exc:
            self._refuse(src, uid, str(exc), step="request")
            return

        size = props.get("size")
        size_int = int(size) if isinstance(size, int) else -1
        file_count = self._count(src)

        receipt_path = self._receipt_path(src)
        body = self._receipt_body(src, size_int, file_count)
        self.client.ensure_dir(self.settings.SHRED_DIR)
        self.client.upload(receipt_path, body.encode("utf-8"))

        # Remove the shred tag from the TARGET — state now lives in the receipt.
        self.client.remove_tag(src.fileid, trigger_tagid)

        log.info(
            "shred request staged",
            extra={
                "audit": "shred.request",
                "fileid": src.fileid,
                "path": src.path,
                "size": size_int,
                "file_count": file_count,
                "receipt": receipt_path,
            },
        )
        self._notify(
            uid,
            "powertools: shred staged",
            f"Shred requested for {src.path}. Confirm by tagging "
            f"{receipt_path} with '{self.settings.SHRED_CONFIRM_TAG}'.",
        )

    def confirm(self, receipt: FileRef, trigger_tagid: int, uid: str) -> None:
        """Step 2: a ``shred-confirm`` tag was placed on a CONFIRM-SHRED receipt."""
        if not self.settings.ENABLE_SHRED:
            log.info("shred disabled — ignoring confirm tag", extra={"fileid": receipt.fileid})
            return

        # The confirm tag must be on a CONFIRM-SHRED-*.md receipt inside SHRED_DIR.
        if not self._is_receipt(receipt):
            log.info(
                "shred-confirm on a non-receipt file — ignoring",
                extra={"audit": "shred.confirm.skip", "fileid": receipt.fileid,
                       "path": receipt.path},
            )
            self.client.remove_tag(receipt.fileid, trigger_tagid)
            return

        # Parse the receipt's machine-readable front-matter statelessly.
        try:
            content = self.client.download(receipt.path)
        except NcApiError as exc:
            log.warning("could not read receipt", extra={"path": receipt.path, "err": str(exc)})
            self.client.remove_tag(receipt.fileid, trigger_tagid)
            return
        fm = self._parse_front_matter(content.decode("utf-8", errors="replace"))
        target_path = fm.get("target_path")
        target_fileid_raw = fm.get("target_fileid")
        if not target_path or not (target_fileid_raw and target_fileid_raw.isdigit()):
            log.info(
                "receipt missing target front-matter — ignoring",
                extra={"audit": "shred.confirm.skip", "path": receipt.path},
            )
            self.client.remove_tag(receipt.fileid, trigger_tagid)
            return
        target_fileid = int(target_fileid_raw)

        # Re-validate scope on the front-matter path BEFORE any network call.
        try:
            self._guard_scope(target_path)
        except ShredRefused as exc:
            self._fail(receipt, uid, trigger_tagid, target_path, str(exc))
            return

        # Re-resolve + re-validate guards on the live target.
        try:
            props = self.client.propfind_props(target_path, user=self.user)
        except NcApiError as exc:
            self._fail(receipt, uid, trigger_tagid, target_path,
                       f"target not found / PROPFIND failed: {exc}")
            return
        try:
            self._guard_props(props)
        except ShredRefused as exc:
            self._fail(receipt, uid, trigger_tagid, target_path, str(exc))
            return

        resolved_fileid = props.get("fileid")
        if resolved_fileid != target_fileid:
            self._fail(
                receipt, uid, trigger_tagid, target_path,
                f"target CHANGED: receipt fileid={target_fileid} but resolved "
                f"fileid={resolved_fileid} — aborting (not the file you confirmed)",
            )
            return

        # Capability check: can we actually purge?
        try:
            caps = self.client.files_capabilities()
        except NcApiError as exc:
            self._fail(receipt, uid, trigger_tagid, target_path,
                       f"could not read capabilities: {exc}")
            return
        undelete = caps.get("undelete", True)
        if undelete and not caps.get("delete_from_trash", True):
            self._fail(
                receipt, uid, trigger_tagid, target_path,
                "delete_from_trash is disabled on this server — cannot permanently "
                "purge from trash (admin policy). No deletion performed.",
            )
            return

        # ---- Purge (DESTRUCTIVE) ---------------------------------------- #
        try:
            self.client.delete_file(target_path, user=self.user)
        except NcApiError as exc:
            self._fail(receipt, uid, trigger_tagid, target_path,
                       f"DELETE of target failed: {exc}")
            return

        purged_from_trash = False
        if undelete:
            # The file is now in trash; find its node by the STABLE fileid and
            # permanently delete it (this also auto-purges its versions).
            try:
                node = self._find_trash_node(target_fileid, target_path)
            except NcApiError as exc:
                self._fail(
                    receipt, uid, trigger_tagid, target_path,
                    f"DELETE succeeded but could not list trash to purge: {exc}. "
                    f"Target is in trash (fileid={target_fileid}) — purge manually.",
                )
                return
            if node is None:
                self._fail(
                    receipt, uid, trigger_tagid, target_path,
                    f"DELETE succeeded but the trash item for fileid={target_fileid} "
                    f"was not found — NOT confirming a permanent purge. Check trash.",
                )
                return
            try:
                self.client.delete_trash_item(node, user=self.user)
            except NcApiError as exc:
                self._fail(
                    receipt, uid, trigger_tagid, target_path,
                    f"permanent trash purge failed for node {node}: {exc}. "
                    f"Target remains in trash — purge manually.",
                )
                return
            purged_from_trash = True
        # else: trash disabled -> the first DELETE was already permanent.

        self._record_success(receipt, uid, trigger_tagid, target_path,
                             target_fileid, purged_from_trash)

    # ----------------------------------------------------------------- #
    # scope guards
    # ----------------------------------------------------------------- #

    def _guard_scope(self, path: str) -> None:
        """Refuse anything not STRICTLY inside SHRED_DIR / containing ``..``."""
        clean = (path or "").strip("/")
        shred_dir = self.settings.SHRED_DIR.strip("/")
        if not clean:
            raise ShredRefused("refused: empty path (account root)")
        if ".." in clean.split("/"):
            raise ShredRefused(f"refused: path contains '..' ({path!r})")
        if not shred_dir:
            # An empty SHRED_DIR would confine to the account root — never allow.
            raise ShredRefused("refused: SHRED_DIR is empty (would target account root)")
        if clean == shred_dir:
            raise ShredRefused(f"refused: target is SHRED_DIR itself ({shred_dir!r})")
        prefix = shred_dir + "/"
        if not clean.startswith(prefix):
            raise ShredRefused(
                f"refused: {path!r} is not strictly inside SHRED_DIR ({shred_dir!r})"
            )

    @staticmethod
    def _guard_props(props: dict[str, object]) -> None:
        """Refuse received shares / non-local (external/group) mounts."""
        mount = props.get("mount_type")
        if isinstance(mount, str) and mount in _NONLOCAL_MOUNTS:
            raise ShredRefused(
                f"refused: target is a '{mount}' mount — a DELETE would only "
                "unmount/unshare it (owner keeps the data), not purge it"
            )
        share_types = props.get("share_types")
        if isinstance(share_types, list) and share_types:
            raise ShredRefused(
                f"refused: target carries share-types {share_types} — it is shared "
                "into/out of this account; a DELETE would only unshare it"
            )

    # ----------------------------------------------------------------- #
    # receipt I/O
    # ----------------------------------------------------------------- #

    def _receipt_path(self, src: FileRef) -> str:
        shred_dir = self.settings.SHRED_DIR.strip("/")
        name = f"{_RECEIPT_PREFIX}{src.fileid}-{_safe_name(src.name)}.md"
        return f"{shred_dir}/{name}"

    def _is_receipt(self, receipt: FileRef) -> bool:
        shred_dir = self.settings.SHRED_DIR.strip("/")
        clean = receipt.path.strip("/")
        return (
            not receipt.is_dir
            and clean.startswith(shred_dir + "/")
            and receipt.name.startswith(_RECEIPT_PREFIX)
            and receipt.name.endswith(".md")
        )

    def _receipt_body(self, src: FileRef, size: int, file_count: int) -> str:
        confirm_tag = self.settings.SHRED_CONFIRM_TAG
        shred_tag = self.settings.SHRED_TAG
        size_str = f"{size} bytes" if size >= 0 else "unknown"
        return f"""# ⚠️ CONFIRM SHRED — PERMANENT, NOT RECOVERABLE

**This will PERMANENTLY purge the following from Nextcloud.** It will NOT be
recoverable from the trash, and its file versions will be removed. This is
**purge-from-Nextcloud only** — it is NOT secure erasure: your storage backend
and especially the **Kopia / hetzbox backups still retain the data** until they
age out. Do not rely on this to make data unrecoverable.

| field | value |
|-------|-------|
| target path | `{src.path}` |
| target fileid | `{src.fileid}` |
| is directory | `{src.is_dir}` |
| size | {size_str} |
| file count | {file_count} |
| requested at | {_now_iso()} |

## To CONFIRM
Add the tag **`{confirm_tag}`** to **THIS** file. The worker will then re-validate
the target and permanently purge it.

## To CANCEL
Remove the `{shred_tag}` tag (already removed from the target) or simply delete
this confirmation file. Unconfirmed requests just sit here harmlessly.

{_FM_OPEN}
target_path: {src.path}
target_fileid: {src.fileid}
target_is_dir: {src.is_dir}
{_FM_CLOSE}
"""

    @staticmethod
    def _parse_front_matter(text: str) -> dict[str, str]:
        """Extract the fenced ``ncpowertools-shred`` block into a flat dict."""
        out: dict[str, str] = {}
        start = text.find(_FM_OPEN)
        if start == -1:
            return out
        rest = text[start + len(_FM_OPEN):]
        end = rest.find(_FM_CLOSE)
        block = rest[:end] if end != -1 else rest
        for line in block.splitlines():
            line = line.strip()
            if not line or ":" not in line:
                continue
            key, val = line.split(":", 1)
            out[key.strip()] = val.strip()
        return out

    # ----------------------------------------------------------------- #
    # trash node matching
    # ----------------------------------------------------------------- #

    def _find_trash_node(self, fileid: int, original_path: str) -> str | None:
        """Find the trash node for ``fileid`` (fallback: original-location)."""
        items = self.client.list_trash(user=self.user)
        for item in items:
            if item.get("fileid") == fileid:
                return str(item["node_name"])
        # Fallback: match by original location, newest deletion-time wins.
        candidates = [
            item
            for item in items
            if str(item.get("original_location") or "").strip("/") == original_path.strip("/")
        ]
        if not candidates:
            return None
        def _del_time(item: dict[str, object]) -> int:
            val = item.get("deletion_time")
            return val if isinstance(val, int) else 0

        candidates.sort(key=_del_time, reverse=True)
        return str(candidates[0]["node_name"])

    # ----------------------------------------------------------------- #
    # outcome recording
    # ----------------------------------------------------------------- #

    def _count(self, src: FileRef) -> int:
        if not src.is_dir:
            return 1
        try:
            return self.client.count_files(src.path, user=self.user)
        except NcApiError:
            return -1

    def _record_success(
        self,
        receipt: FileRef,
        uid: str,
        trigger_tagid: int,
        target_path: str,
        target_fileid: int,
        purged_from_trash: bool,
    ) -> None:
        ts = _ts_compact()
        new_name = f"{_SHREDDED_PREFIX}{_safe_name(target_path.rsplit('/', 1)[-1])}-{ts}.md"
        shred_dir = self.settings.SHRED_DIR.strip("/")
        new_path = f"{shred_dir}/{new_name}"
        trash_note = (
            "emptied from trash (versions auto-purged)"
            if purged_from_trash
            else "trash disabled — first DELETE was already permanent"
        )
        body = f"""# ✅ SHREDDED — permanently purged from Nextcloud

| field | value |
|-------|-------|
| target path | `{target_path}` |
| target fileid | `{target_fileid}` |
| completed at | {_now_iso()} |
| trash | {trash_note} |

Reminder: this purged it from Nextcloud only. Backups (Kopia / hetzbox) and the
storage layer may still retain the data until they age out.
"""
        # Write the SHREDDED receipt, then remove the now-obsolete CONFIRM receipt
        # and the confirm tag. Best-effort cleanup — the purge already succeeded.
        try:
            self.client.upload(new_path, body.encode("utf-8"))
            if receipt.path.strip("/") != new_path:
                try:
                    self.client.delete_file(receipt.path, user=self.user)
                except NcApiError:
                    log.warning("could not remove old CONFIRM receipt",
                                extra={"path": receipt.path})
        except NcApiError:
            log.warning("could not write SHREDDED receipt", extra={"path": new_path})
        self.client.remove_tag(receipt.fileid, trigger_tagid)

        log.info(
            "shred completed",
            extra={
                "audit": "shred.confirm.purged",
                "target_path": target_path,
                "target_fileid": target_fileid,
                "purged_from_trash": purged_from_trash,
                "receipt": new_path,
            },
        )
        self._notify(
            uid,
            "powertools: shred completed",
            f"Permanently purged {target_path} from Nextcloud "
            f"({trash_note}). Backups may still retain it.",
        )

    def _fail(
        self,
        receipt: FileRef,
        uid: str,
        trigger_tagid: int,
        target_path: str,
        message: str,
    ) -> None:
        """Step-2 failure: write a FAILED note, keep state, do NOT report success."""
        ts = _ts_compact()
        shred_dir = self.settings.SHRED_DIR.strip("/")
        name = f"{_FAILED_PREFIX}{_safe_name(target_path.rsplit('/', 1)[-1])}-{ts}.md"
        new_path = f"{shred_dir}/{name}"
        body = f"""# ❌ SHRED FAILED — no (or partial) deletion

The shred of `{target_path}` did NOT complete successfully and was NOT confirmed.

```
{message}
```

Reason recorded at {_now_iso()}. Review, fix, and re-stage if intended.
"""
        try:
            self.client.upload(new_path, body.encode("utf-8"))
        except NcApiError:
            log.warning("could not write FAILED receipt", extra={"path": new_path})
        # Remove the confirm tag so the poller doesn't hot-loop the failure;
        # the FAILED note + log preserve the state.
        self.client.remove_tag(receipt.fileid, trigger_tagid)
        log.error(
            "shred failed",
            extra={
                "audit": "shred.confirm.failed",
                "target_path": target_path,
                "error": message,
                "receipt": new_path,
            },
        )
        self._notify(
            uid,
            "powertools: shred FAILED",
            f"Shred of {target_path} failed: {message}",
        )

    def _refuse(self, src: FileRef, uid: str, message: str, *, step: str) -> None:
        """Step-1 (or guard) refusal: log + optional notify, NEVER delete/tag-remove.

        We deliberately do NOT remove the trigger tag on a refusal so the
        refusal is visible in the NC UI (the file keeps its shred tag); the
        owner removes it after reading the log/notification.
        """
        log.warning(
            "shred refused",
            extra={
                "audit": f"shred.{step}.refused",
                "fileid": src.fileid,
                "path": src.path,
                "reason": message,
            },
        )
        self._notify(uid, "powertools: shred refused", f"{src.path}: {message}")

    def _notify(self, uid: str, short: str, long: str) -> None:
        try:
            self.client.notify(uid, short, long)
        except Exception:  # noqa: BLE001 - a failed notify must never break shred
            log.warning("shred notify failed", extra={"uid": uid})

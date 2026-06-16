"""NextcloudClient — all WebDAV/OCS calls behind one httpx.Client.

URLs, methods and headers follow CONTEXT.md exactly. The client is synchronous
(one worker, low concurrency). Non-2xx responses raise :class:`NcApiError` with
the status, URL and a short body snippet.

Path handling: WebDAV paths are relative to the target user's root and are
percent-encoded per segment (``quote(path, safe="/")``) while keeping ``/`` as
the separator (CONTEXT.md §2).
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import IO, TYPE_CHECKING
from urllib.parse import quote

import httpx

from ..errors import NcApiError
from ..logging import get_logger
from ..models import FileRef, TagSpec
from . import webdav_xml as xml

if TYPE_CHECKING:
    from ..config import Settings

log = get_logger("nextcloud.client")

_OCS_HEADERS = {"OCS-APIRequest": "true", "Accept": "application/json"}
_SNIPPET_LEN = 300


class NextcloudClient:
    """Wraps a single ``httpx.Client`` with Basic auth and the NC base URL."""

    def __init__(self, settings: Settings, *, client: httpx.Client | None = None) -> None:
        self.settings = settings
        self.base_url = settings.NEXTCLOUD_URL.rstrip("/")
        self.user = settings.TARGET_USER or settings.NC_USER
        self._version: tuple[int, int, int] | None = None
        self._client = client or httpx.Client(
            base_url=self.base_url,
            auth=(settings.NC_USER, settings.NC_APP_PASSWORD),
            timeout=httpx.Timeout(30.0, read=120.0),
            follow_redirects=False,
        )

    # ----------------------------------------------------------------- #
    # lifecycle
    # ----------------------------------------------------------------- #

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> NextcloudClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ----------------------------------------------------------------- #
    # internals
    # ----------------------------------------------------------------- #

    def _files_url(self, path: str, user: str | None = None) -> str:
        user = user or self.user
        encoded = quote(path.strip("/"), safe="/")
        return f"/remote.php/dav/files/{user}/{encoded}"

    @staticmethod
    def _snippet(resp: httpx.Response) -> str:
        try:
            text = resp.text
        except Exception:  # pragma: no cover - defensive
            return ""
        return text[:_SNIPPET_LEN]

    def _check(self, resp: httpx.Response, *, ok: tuple[int, ...]) -> httpx.Response:
        if resp.status_code not in ok:
            raise NcApiError(
                f"Nextcloud request failed ({resp.request.method})",
                status=resp.status_code,
                url=str(resp.request.url),
                snippet=self._snippet(resp),
            )
        return resp

    def _request(
        self,
        method: str,
        url: str,
        *,
        ok: tuple[int, ...],
        **kwargs: object,
    ) -> httpx.Response:
        resp = self._client.request(method, url, **kwargs)  # type: ignore[arg-type]
        return self._check(resp, ok=ok)

    # ----------------------------------------------------------------- #
    # capabilities / version
    # ----------------------------------------------------------------- #

    def capabilities(self) -> tuple[int, int, int]:
        """Return the server ``(major, minor, micro)`` version (cached)."""
        if self._version is not None:
            return self._version
        resp = self._request(
            "GET",
            "/ocs/v2.php/cloud/capabilities?format=json",
            headers=_OCS_HEADERS,
            ok=(200,),
        )
        data = resp.json()
        try:
            version = data["ocs"]["data"]["version"]
            self._version = (
                int(version["major"]),
                int(version["minor"]),
                int(version["micro"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise NcApiError(
                f"Could not parse capabilities version: {exc}",
                status=resp.status_code,
                url=str(resp.request.url),
                snippet=self._snippet(resp),
            ) from exc
        return self._version

    @property
    def major(self) -> int:
        return self.capabilities()[0]

    def files_capabilities(self) -> dict[str, bool]:
        """Return the ``files`` capability flags relevant to shred (F5).

        Reads ``ocs.data.capabilities.files`` and returns the trash/version
        flags: ``undelete`` (trash enabled), ``delete_from_trash`` (permanent
        purge from trash allowed), ``versioning``, ``version_deletion``.
        **Missing keys default to True** ("allowed/default on older servers")
        per the verified NC facts — except when the whole ``files`` block is
        absent, in which case we still default each to True so an older server
        isn't wrongly treated as trash-disabled.
        """
        resp = self._request(
            "GET",
            "/ocs/v2.php/cloud/capabilities?format=json",
            headers=_OCS_HEADERS,
            ok=(200,),
        )
        try:
            files = resp.json()["ocs"]["data"]["capabilities"].get("files", {})
        except (KeyError, TypeError, ValueError):
            files = {}
        if not isinstance(files, dict):
            files = {}

        def flag(key: str) -> bool:
            val = files.get(key, True)  # missing => allowed/default True
            return bool(val)

        return {
            "undelete": flag("undelete"),
            "delete_from_trash": flag("delete_from_trash"),
            "versioning": flag("versioning"),
            "version_deletion": flag("version_deletion"),
        }

    # ----------------------------------------------------------------- #
    # download / upload
    # ----------------------------------------------------------------- #

    def download(self, path: str) -> bytes:
        """GET file contents as bytes."""
        resp = self._request("GET", self._files_url(path), ok=(200,))
        return resp.content

    def download_to(self, path: str, dest: Path) -> Path:
        """Stream a file to ``dest`` on disk."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        url = self._files_url(path)
        with self._client.stream("GET", url) as resp:
            self._check(resp, ok=(200,))
            with dest.open("wb") as fh:
                for chunk in resp.iter_bytes():
                    fh.write(chunk)
        return dest

    def download_dir_as_zip(self, path: str, dest: Path) -> Path:
        """Download a *folder* as a zip archive to ``dest`` on disk.

        Uses Nextcloud's directory-GET extension: a GET on a collection with
        ``Accept: application/zip`` streams the folder packed as a zip. We use
        this for the compress actions on folders so the handler can operate on a
        single local file/dir (CONTEXT.md §2). The pipeline then extracts it
        locally to reconstruct the tree before re-compressing.
        """
        dest.parent.mkdir(parents=True, exist_ok=True)
        url = self._files_url(path)
        with self._client.stream("GET", url, headers={"Accept": "application/zip"}) as resp:
            self._check(resp, ok=(200,))
            with dest.open("wb") as fh:
                for chunk in resp.iter_bytes():
                    fh.write(chunk)
        return dest

    def last_modified(self, path: str, user: str | None = None) -> datetime | None:
        """Return a file's modification time (PROPFIND ``getlastmodified``) or None.

        Used by the Immich integration (F6) to set ``fileCreatedAt`` /
        ``fileModifiedAt`` from the WebDAV mtime. Best-effort: an unparseable or
        missing date returns ``None`` (the caller falls back to ``now()``).
        """
        body = xml.build_lastmodified_propfind()
        try:
            resp = self._request(
                "PROPFIND",
                self._files_url(path, user=user),
                content=body,
                headers={"Content-Type": "application/xml", "Depth": "0"},
                ok=(207,),
            )
        except NcApiError:
            return None
        return xml.parse_lastmodified(resp.content)

    def upload(self, path: str, data: bytes | IO[bytes]) -> None:
        """PUT raw bytes (or a file object) to ``path`` (overwrites if present)."""
        self._request("PUT", self._files_url(path), content=data, ok=(200, 201, 204))

    def ensure_dir(self, path: str) -> None:
        """Ensure a collection exists.

        On NC32+ a single MKCOL with ``X-NC-WebDAV-AutoMkcol: 1`` creates missing
        parents; on NC30/31 we MKCOL each level (parents-first). 405 (exists) is
        treated as success.
        """
        path = path.strip("/")
        if not path:
            return
        if self.major >= 32:
            self._request(
                "MKCOL",
                self._files_url(path),
                headers={"X-NC-WebDAV-AutoMkcol": "1"},
                ok=(201, 405),
            )
            return
        parts = path.split("/")
        for i in range(1, len(parts) + 1):
            sub = "/".join(parts[:i])
            self._request("MKCOL", self._files_url(sub), ok=(201, 405))

    # ----------------------------------------------------------------- #
    # shred support (DESTRUCTIVE — used only by ShredService, F5)
    # ----------------------------------------------------------------- #

    def propfind_props(self, path: str, user: str | None = None) -> dict[str, object]:
        """PROPFIND (Depth 0) a shred target for its scope-guard props.

        Returns the dict from :func:`webdav_xml.parse_shred_props`:
        ``fileid``, ``size``, ``share_types``, ``mount_type``, ``is_dir``.
        Used to verify identity + refuse received shares / external mounts
        before a destructive DELETE.
        """
        body = xml.build_shred_propfind()
        resp = self._request(
            "PROPFIND",
            self._files_url(path, user=user),
            content=body,
            headers={"Content-Type": "application/xml", "Depth": "0"},
            ok=(207,),
        )
        return xml.parse_shred_props(resp.content)

    def count_files(self, path: str, user: str | None = None) -> int:
        """Count non-collection members under a folder (Depth infinity).

        Best-effort for the receipt's "file count". Returns the number of
        responses that are NOT collections (the folder itself is excluded).
        """
        body = xml.build_count_propfind()
        resp = self._request(
            "PROPFIND",
            self._files_url(path, user=user),
            content=body,
            headers={"Content-Type": "application/xml", "Depth": "infinity"},
            ok=(207,),
        )
        # Reuse the file-report parser shape: count refs that are not dirs.
        refs = xml.parse_file_report(resp.content, user=user or self.user)
        return sum(1 for r in refs if not r.is_dir)

    def delete_file(self, path: str, user: str | None = None) -> None:
        """DELETE a file/folder in the user's files namespace -> trash (204).

        Plain WebDAV DELETE moves the target to the trashbin when trash is
        enabled (recoverable); recursive for folders. **Destructive** — only
        called by :class:`~ncpowertools.shred.ShredService` after every guard
        has passed.
        """
        self._request("DELETE", self._files_url(path, user=user), ok=(204,))

    def list_trash(self, user: str | None = None) -> list[dict[str, object]]:
        """PROPFIND the user's trashbin -> list of trash-item dicts.

        Each item carries ``node_name`` (taken from the href tail, NEVER
        constructed), ``fileid`` (stable, matches the live file's id),
        ``original_location`` and ``deletion_time``.
        """
        user = user or self.user
        body = xml.build_trash_propfind()
        resp = self._request(
            "PROPFIND",
            f"/remote.php/dav/trashbin/{user}/trash",
            content=body,
            headers={"Content-Type": "application/xml", "Depth": "1"},
            ok=(207,),
        )
        return xml.parse_trash_items(resp.content)

    def delete_trash_item(self, node_name: str, user: str | None = None) -> None:
        """Permanently purge one trash item (204). Also purges its versions.

        ``node_name`` MUST come from a :meth:`list_trash` item's ``node_name``
        (the href tail), never constructed. A ``403`` means
        ``delete_from_trash`` is admin-disabled and surfaces as an
        :class:`NcApiError`.
        """
        user = user or self.user
        encoded = quote(node_name, safe="")
        self._request(
            "DELETE",
            f"/remote.php/dav/trashbin/{user}/trash/{encoded}",
            ok=(204,),
        )

    # ----------------------------------------------------------------- #
    # fileid resolution
    # ----------------------------------------------------------------- #

    def resolve_fileid(self, fileid: int, user: str | None = None) -> FileRef | None:
        """Resolve a fileid to a :class:`FileRef` via the WebDAV SEARCH method.

        Used for the **webhook path**, where only a fileid is delivered (the poller
        path already carries a resolved ``FileRef`` and never calls this).

        We use SEARCH — **not** the old ``oc:filter-files`` / ``<oc:fileid>`` REPORT,
        which Nextcloud silently ignores (verified live on NC 33.0.5: empty
        multistatus), and **not** ownCloud's ``/remote.php/dav/meta/{fileid}``
        endpoint, which does not exist in Nextcloud (no ``Meta`` collection in NC's
        DAV ``RootCollection``). The documented, supported NC resolver is a ``SEARCH``
        on ``/remote.php/dav/`` matching ``<oc:fileid>`` in a ``<d:where>`` scoped to
        the user's ``/files/<user>`` tree; the response multistatus is the same shape
        as a file REPORT (``<d:href>`` + ``<oc:fileid>`` + ``<d:resourcetype>``), so we
        reuse :func:`parse_file_report` and get ``is_dir`` in the same round-trip.
        """
        user = user or self.user
        body = xml.build_fileid_search(fileid, user=user)
        resp = self._request(
            "SEARCH",
            "/remote.php/dav/",
            content=body,
            headers={"Content-Type": "application/xml"},
            ok=(207,),
        )
        refs = xml.parse_file_report(resp.content, user=user)
        return refs[0] if refs else None

    # ----------------------------------------------------------------- #
    # system tags
    # ----------------------------------------------------------------- #

    def list_tags(self) -> list[TagSpec]:
        """List all system tags via PROPFIND on /remote.php/dav/systemtags/."""
        body = xml.build_systemtags_propfind()
        resp = self._request(
            "PROPFIND",
            "/remote.php/dav/systemtags/",
            content=body,
            headers={"Content-Type": "application/xml", "Depth": "1"},
            ok=(207,),
        )
        return xml.parse_systemtags(resp.content)

    def tags_on_file(self, fileid: int) -> list[TagSpec]:
        """List the tags assigned to one file via the relations PROPFIND."""
        body = xml.build_relations_propfind()
        resp = self._request(
            "PROPFIND",
            f"/remote.php/dav/systemtags-relations/files/{fileid}",
            content=body,
            headers={"Content-Type": "application/xml", "Depth": "1"},
            ok=(207,),
        )
        return xml.parse_systemtags(resp.content)

    def ensure_tag(self, name: str) -> TagSpec:
        """Return the tag named ``name``, creating it if missing (idempotent).

        Pattern (CONTEXT.md §3): list first; create on miss; parse the new id
        from ``Content-Location``; treat 409 Conflict as "already exists" and
        re-list to find the id.
        """
        for tag in self.list_tags():
            if tag.name == name:
                return tag
        resp = self._client.post(
            "/remote.php/dav/systemtags/",
            json={"name": name, "userVisible": True, "userAssignable": True},
        )
        if resp.status_code == 201:
            tag_id = xml.parse_content_location_id(resp.headers.get("Content-Location", ""))
            return TagSpec(id=tag_id, name=name)
        if resp.status_code == 409:
            for tag in self.list_tags():
                if tag.name == name:
                    return tag
            raise NcApiError(
                "Tag reported as existing (409) but not found on re-list",
                status=409,
                url=str(resp.request.url),
            )
        raise NcApiError(
            "Failed to create system tag",
            status=resp.status_code,
            url=str(resp.request.url),
            snippet=self._snippet(resp),
        )

    def search_by_tag(self, tagid: int, user: str | None = None) -> list[FileRef]:
        """Find all files carrying ``tagid`` via the oc:systemtag REPORT."""
        user = user or self.user
        body = xml.build_systemtag_report(tagid)
        resp = self._request(
            "REPORT",
            self._files_url("", user=user),
            content=body,
            headers={"Content-Type": "application/xml"},
            ok=(207,),
        )
        return xml.parse_file_report(resp.content, user=user)

    def assign_tag(self, fileid: int, tagid: int) -> None:
        """Assign a tag to a file (PUT relation, empty body) -> 201."""
        self._request(
            "PUT",
            f"/remote.php/dav/systemtags-relations/files/{fileid}/{tagid}",
            ok=(201, 409),  # 409 = already assigned -> idempotent success
        )

    def remove_tag(self, fileid: int, tagid: int) -> None:
        """Remove a tag from a file (DELETE relation) -> 204."""
        self._request(
            "DELETE",
            f"/remote.php/dav/systemtags-relations/files/{fileid}/{tagid}",
            ok=(204, 404),  # 404 = not assigned -> idempotent success
        )

    # ----------------------------------------------------------------- #
    # notifications (OCS admin_notifications) — best-effort, optional
    # ----------------------------------------------------------------- #

    def notify(self, uid: str, short: str, long: str = "") -> None:
        """Send an OCS admin notification, only when ``NOTIFY`` is enabled.

        Uses the admin credentials. Failures are swallowed (logged) — a failed
        notification must never break the pipeline.
        """
        if not self.settings.NOTIFY:
            return
        url = (
            f"/ocs/v2.php/apps/notifications/api/v2/admin_notifications/{quote(uid, safe='')}"
        )
        data = {"shortMessage": short[:255]}
        if long:
            data["longMessage"] = long[:4000]
        try:
            resp = self._client.post(
                url,
                headers=_OCS_HEADERS,
                data=data,
                auth=(self.settings.NC_ADMIN_USER, self.settings.NC_ADMIN_PASSWORD),
            )
            if resp.status_code not in (200,):
                log.warning(
                    "notify failed",
                    extra={"status": resp.status_code, "uid": uid},
                )
        except httpx.HTTPError as exc:
            log.warning("notify error", extra={"error": str(exc), "uid": uid})

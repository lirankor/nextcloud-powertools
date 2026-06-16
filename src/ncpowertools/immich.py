"""``immich`` — push Nextcloud photos/videos to a separate Immich server (F6).

A NON-destructive integration power tool. When a file (or folder) carries the
``immich`` tag (or an ``immich-<album>`` tag), the pipeline downloads a COPY of
the bytes from Nextcloud and uploads it to an Immich server over its REST API.
The Nextcloud original is **always kept**; only the trigger tag is removed on
success. Re-runs are safe: Immich dedupes by SHA-1 checksum (via the
bulk-upload-check precheck + an ``x-immich-checksum`` header), so a file already
in Immich is not uploaded twice but its existing asset id is still harvested so
it can be added to the requested album.

This module is the thin client for the Immich API (verified against the current
OpenAPI, forward-stable to 3.0). It holds its own :class:`httpx.Client` (base
``IMMICH_URL``, ``x-api-key`` auth) and is a context manager. It is injectable
for tests (pass ``client=...``). The pipeline (:mod:`ncpowertools.pipeline`)
owns the WebDAV download + the per-file orchestration; this module only speaks
to Immich.

Endpoints used (base path ``/api``):

* ``GET  /server/ping``     -> ``{"res":"pong"}`` (unauth reachability probe)
* ``GET  /server/version``  -> ``{"major","minor","patch"}``
* ``GET  /server/media-types`` -> ``{"image":[...],"video":[...],"sidecar":[...]}``
  (cached at first use to build the directory-walk allow-list)
* ``POST /assets/bulk-upload-check`` -> dedup precheck by checksum
* ``POST /assets``          -> multipart upload (``assetData`` + device/time fields)
* ``GET  /albums``          -> list albums (NO server-side name filter; matched here)
* ``POST /albums``          -> create album (optionally with ``assetIds``)
* ``PUT  /albums/{id}/assets`` -> add assets to an existing album
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

from .logging import get_logger

if TYPE_CHECKING:
    from .config import Settings

log = get_logger("immich")

# A sane hardcoded allow-list used ONLY as a fallback if /server/media-types is
# unreachable at first use. Lowercased, no leading dot. Covers the common photo
# + video set Immich accepts; the live media-types call (when reachable) is the
# source of truth and overrides this.
_FALLBACK_IMAGE_EXTS: frozenset[str] = frozenset(
    {
        "jpg", "jpeg", "png", "gif", "webp", "tif", "tiff", "bmp", "heic",
        "heif", "avif", "dng", "cr2", "cr3", "nef", "arw", "raf", "orf", "rw2",
        "pef", "srw", "jxl", "psd", "svg", "ico",
    }
)
_FALLBACK_VIDEO_EXTS: frozenset[str] = frozenset(
    {
        "mp4", "mov", "m4v", "webm", "mkv", "avi", "wmv", "flv", "3gp", "mpg",
        "mpeg", "m2ts", "mts", "ts", "insv", "mp2t",
    }
)


def sha1_of_file(path: Path) -> str:
    """Return the lowercase hex SHA-1 of a file's contents (streamed)."""
    h = hashlib.sha1()  # noqa: S324 - Immich uses SHA-1 for asset checksums (not security)
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def iso_utc(dt: datetime | None) -> str:
    """Format ``dt`` as an Immich-friendly ISO-8601 ``...Z`` string.

    ``None`` -> now (UTC). Naive datetimes are treated as UTC.
    """
    if dt is None:
        dt = datetime.now(UTC)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")


class ImmichError(Exception):
    """Raised on an unexpected Immich API response (status / shape)."""


class ImmichService:
    """Thin Immich REST client (see module docstring). Context-manageable."""

    def __init__(self, settings: Settings, *, client: httpx.Client | None = None) -> None:
        self.settings = settings
        self.base_url = settings.IMMICH_URL.rstrip("/")
        self._media_types: dict[str, frozenset[str]] | None = None
        self._client = client or httpx.Client(
            base_url=self.base_url,
            headers={"x-api-key": settings.IMMICH_API_KEY},
            timeout=httpx.Timeout(30.0, read=300.0),
            follow_redirects=False,
        )

    # ----------------------------------------------------------------- #
    # lifecycle
    # ----------------------------------------------------------------- #

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> ImmichService:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ----------------------------------------------------------------- #
    # internals
    # ----------------------------------------------------------------- #

    def _check(self, resp: httpx.Response, *, ok: tuple[int, ...]) -> httpx.Response:
        if resp.status_code not in ok:
            body = resp.text[:300] if resp.text else ""
            raise ImmichError(
                f"Immich {resp.request.method} {resp.request.url.path} "
                f"-> {resp.status_code}: {body}"
            )
        return resp

    # ----------------------------------------------------------------- #
    # server probes
    # ----------------------------------------------------------------- #

    def ping(self) -> bool:
        """``GET /api/server/ping`` -> True iff the server answers ``pong``."""
        resp = self._check(self._client.get("/api/server/ping"), ok=(200,))
        return bool(resp.json().get("res") == "pong")

    def version(self) -> str:
        """``GET /api/server/version`` -> ``"major.minor.patch"`` string."""
        resp = self._check(self._client.get("/api/server/version"), ok=(200,))
        v = resp.json()
        return f"{v.get('major', 0)}.{v.get('minor', 0)}.{v.get('patch', 0)}"

    def media_types(self) -> dict[str, frozenset[str]]:
        """Return accepted ``{"image","video","sidecar"}`` ext sets (cached).

        Calls ``GET /api/server/media-types`` once and caches it. The API returns
        MIME-like extensions WITH a leading dot (``".jpg"``); we normalize to
        lowercased, dot-stripped exts. If the call fails we fall back to a sane
        hardcoded set so the directory-walk allow-list still works offline.
        """
        if self._media_types is not None:
            return self._media_types
        try:
            resp = self._check(self._client.get("/api/server/media-types"), ok=(200,))
            data = resp.json()
            self._media_types = {
                "image": _norm_exts(data.get("image", [])),
                "video": _norm_exts(data.get("video", [])),
                "sidecar": _norm_exts(data.get("sidecar", [])),
            }
        except (httpx.HTTPError, ImmichError, ValueError) as exc:
            log.warning(
                "media-types unreachable; using fallback allow-list",
                extra={"error": str(exc)},
            )
            self._media_types = {
                "image": _FALLBACK_IMAGE_EXTS,
                "video": _FALLBACK_VIDEO_EXTS,
                "sidecar": frozenset({"xmp"}),
            }
        return self._media_types

    def is_accepted_media(self, name: str) -> bool:
        """Whether a file name's extension is an accepted image OR video type.

        Sidecars (``.xmp``) are intentionally NOT uploaded as standalone assets.
        """
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if not ext:
            return False
        mt = self.media_types()
        return ext in mt["image"] or ext in mt["video"]

    # ----------------------------------------------------------------- #
    # assets
    # ----------------------------------------------------------------- #

    def bulk_check(self, items: list[tuple[str, str]]) -> dict[str, tuple[str, str | None]]:
        """Dedup precheck. ``items`` = list of ``(corr_id, sha1_hex)``.

        ``POST /api/assets/bulk-upload-check`` with
        ``{"assets":[{"id","checksum"}]}``. Returns a map
        ``corr_id -> (action, asset_id)`` where ``action`` is ``"accept"`` (not
        present, upload it) or ``"reject"`` (duplicate; ``asset_id`` is the
        existing Immich asset id, used to still add it to the album).
        """
        if not items:
            return {}
        payload = {"assets": [{"id": cid, "checksum": csum} for cid, csum in items]}
        resp = self._check(
            self._client.post("/api/assets/bulk-upload-check", json=payload),
            ok=(200, 201),
        )
        out: dict[str, tuple[str, str | None]] = {}
        for r in resp.json().get("results", []):
            cid = r.get("id")
            if cid is None:
                continue
            out[cid] = (r.get("action", "accept"), r.get("assetId"))
        return out

    def upload(
        self,
        local_path: Path,
        *,
        device_asset_id: str,
        file_created_at: datetime | None,
        file_modified_at: datetime | None,
        checksum: str,
    ) -> tuple[str, str]:
        """Upload one asset. Returns ``(asset_id, status)``.

        ``POST /api/assets`` multipart/form-data. ``status`` is ``"created"``
        (201) or ``"duplicate"`` (200 — ``asset_id`` is the existing asset). We
        do NOT set Content-Type (httpx writes the multipart boundary). The
        ``x-immich-checksum`` header lets Immich verify/dedupe by SHA-1.
        """
        data = {
            "deviceAssetId": device_asset_id,
            "deviceId": self.settings.IMMICH_DEVICE_ID,
            "fileCreatedAt": iso_utc(file_created_at),
            "fileModifiedAt": iso_utc(file_modified_at),
        }
        with local_path.open("rb") as fh:
            files = {"assetData": (local_path.name, fh, "application/octet-stream")}
            resp = self._check(
                self._client.post(
                    "/api/assets",
                    data=data,
                    files=files,
                    headers={"x-immich-checksum": checksum},
                ),
                ok=(200, 201),
            )
        body = resp.json()
        asset_id = body.get("id")
        if not asset_id:
            raise ImmichError(f"upload response missing asset id: {body}")
        status = body.get("status") or ("duplicate" if resp.status_code == 200 else "created")
        return str(asset_id), str(status)

    # ----------------------------------------------------------------- #
    # albums
    # ----------------------------------------------------------------- #

    def list_albums(self) -> list[dict[str, object]]:
        """``GET /api/albums`` -> list of album dicts (``id``, ``albumName``, ...)."""
        resp = self._check(self._client.get("/api/albums"), ok=(200,))
        data = resp.json()
        return list(data) if isinstance(data, list) else []

    def find_or_create_album(self, name: str, asset_ids: list[str]) -> str:
        """Return the album id for ``name``, creating it (with ``asset_ids``) if absent.

        Album names are NOT unique in Immich and there is no server-side name
        filter on the stable API, so we list + match ``albumName`` client-side.
        When MULTIPLE albums share the name we pick the OLDEST (by ``createdAt``)
        and log the ambiguity, then ADD the assets to it. When none match we
        create one with the assets in a single ``POST /api/albums``.
        """
        matches = [a for a in self.list_albums() if a.get("albumName") == name]
        if matches:
            if len(matches) > 1:
                log.warning(
                    "multiple albums share a name; using the oldest",
                    extra={"album": name, "count": len(matches)},
                )
                matches.sort(key=lambda a: str(a.get("createdAt") or ""))
            album_id = str(matches[0]["id"])
            if asset_ids:
                self.add_to_album(album_id, asset_ids)
            return album_id
        # No match -> create (optionally with the assets in one call).
        payload: dict[str, object] = {"albumName": name}
        if asset_ids:
            payload["assetIds"] = asset_ids
        resp = self._check(self._client.post("/api/albums", json=payload), ok=(200, 201))
        album_id = resp.json().get("id")
        if not album_id:
            raise ImmichError(f"create-album response missing id for {name!r}")
        log.info("created Immich album", extra={"album": name, "id": album_id})
        return str(album_id)

    def add_to_album(self, album_id: str, asset_ids: list[str]) -> None:
        """``PUT /api/albums/{id}/assets`` -> add assets (``error:"duplicate"`` is fine)."""
        if not asset_ids:
            return
        resp = self._check(
            self._client.put(f"/api/albums/{album_id}/assets", json={"ids": asset_ids}),
            ok=(200, 201),
        )
        # Each result is {id, success, error?}; "duplicate" = already in album = ok.
        for r in resp.json() if isinstance(resp.json(), list) else []:
            if not r.get("success") and r.get("error") not in (None, "duplicate"):
                log.warning(
                    "could not add asset to album",
                    extra={"album": album_id, "asset": r.get("id"), "error": r.get("error")},
                )


def _norm_exts(values: list[str]) -> frozenset[str]:
    """Normalize a list of Immich media-type extensions to dot-stripped lowercase."""
    return frozenset(v.lower().lstrip(".") for v in values if isinstance(v, str) and v)

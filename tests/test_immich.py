"""Tests for the Immich integration (F6) — all respx-mocked, never live.

Covers:
* disabled by default (an ``immich`` tag is ignored — no Immich calls);
* the parameterized prefix-tag parser (``immich`` -> None, ``immich-Trip`` ->
  album, ``immich-`` -> None);
* the poller picking up ``immich`` + ``immich-*`` tags via list_tags + prefix;
* single-file upload multipart fields + checksum header; the duplicate (precheck
  reject) path skipping the upload but still adding the harvested asset to the album;
* album find-or-create (list -> miss -> POST) and add-to-existing (list -> match ->
  PUT), incl. multiple same-name albums -> oldest chosen + logged;
* directory walk with mixed media/non-media (media-types filter) + MAX_FILES;
* failure: Immich 500 on upload -> trigger tag KEPT + ERROR_TAG behavior, no crash.
"""

from __future__ import annotations

from datetime import UTC
from pathlib import Path

import httpx
import respx

from ncpowertools import locking
from ncpowertools.config import (
    Settings,
    immich_album_from_tag,
    is_immich_tag,
)
from ncpowertools.immich import ImmichService, iso_utc, sha1_of_file
from ncpowertools.models import FileRef, TagEvent, TagSpec
from ncpowertools.nextcloud import NextcloudClient
from ncpowertools.pipeline import Pipeline
from ncpowertools.poller import Poller

NC = "https://cloud.example.com"
IMM = "https://immich.example.com"
USER = "powertools"


def _settings(tmp_path: Path, **over: object) -> Settings:
    kw: dict[str, object] = {
        "NEXTCLOUD_URL": NC,
        "NC_USER": USER,
        "NC_APP_PASSWORD": "app-pw",
        "WORK_DIR": str(tmp_path / "work"),
        "ERROR_TAG": "",
        "NOTIFY": False,
        "POLL_INTERVAL": 0,
        "ENABLE_IMMICH": True,
        "IMMICH_URL": IMM,
        "IMMICH_API_KEY": "secret-key",
    }
    kw.update(over)
    return Settings(**kw)  # type: ignore[arg-type]


def _caps(mock: respx.MockRouter) -> None:
    mock.get(f"{NC}/ocs/v2.php/cloud/capabilities").mock(
        return_value=httpx.Response(
            200, json={"ocs": {"data": {"version": {"major": 33, "minor": 0, "micro": 0}}}}
        )
    )


def _tags_relation(*tags: tuple[int, str]) -> bytes:
    rows = "".join(
        f"<d:response><d:href>/remote.php/dav/systemtags-relations/files/x/{tid}</d:href>"
        f"<d:propstat><d:prop><oc:id>{tid}</oc:id><oc:display-name>{name}</oc:display-name>"
        "</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
        for tid, name in tags
    )
    return (
        '<?xml version="1.0"?>'
        '<d:multistatus xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">'
        f"{rows}</d:multistatus>"
    ).encode()


def _lastmodified(date: str = "Wed, 01 Jan 2025 12:00:00 GMT") -> bytes:
    return (
        '<?xml version="1.0"?>'
        '<d:multistatus xmlns:d="DAV:">'
        f"<d:response><d:href>/remote.php/dav/files/{USER}/x</d:href>"
        f"<d:propstat><d:prop><d:getlastmodified>{date}</d:getlastmodified></d:prop>"
        "<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
        "</d:multistatus>"
    ).encode()


def _media_types(mock: respx.MockRouter) -> None:
    mock.get(f"{IMM}/api/server/media-types").mock(
        return_value=httpx.Response(
            200,
            json={
                "image": [".jpg", ".jpeg", ".png", ".heic"],
                "video": [".mp4", ".mov"],
                "sidecar": [".xmp"],
            },
        )
    )


# --------------------------------------------------------------------------- #
# parser unit tests
# --------------------------------------------------------------------------- #


def test_album_parser() -> None:
    assert immich_album_from_tag("immich", "immich") is None
    assert immich_album_from_tag("immich-Trip 2025", "immich") == "Trip 2025"
    assert immich_album_from_tag("immich-Summer Trip", "immich") == "Summer Trip"
    assert immich_album_from_tag("immich-", "immich") is None
    # Non-immich tag -> None.
    assert immich_album_from_tag("render", "immich") is None


def test_is_immich_tag() -> None:
    assert is_immich_tag("immich", "immich")
    assert is_immich_tag("immich-Trip", "immich")
    assert is_immich_tag("immich-", "immich")
    assert not is_immich_tag("render", "immich")
    assert not is_immich_tag("immichx", "immich")  # no separator -> not a match
    # Custom base tag respected.
    assert is_immich_tag("photos-Trip", "photos")
    assert not is_immich_tag("immich", "photos")


def test_iso_utc_format() -> None:
    from datetime import datetime

    dt = datetime(2025, 1, 2, 3, 4, 5, tzinfo=UTC)
    assert iso_utc(dt) == "2025-01-02T03:04:05.000Z"
    assert iso_utc(None).endswith("Z")


# --------------------------------------------------------------------------- #
# disabled by default
# --------------------------------------------------------------------------- #


@respx.mock
def test_disabled_ignores_immich_tag(tmp_path: Path) -> None:
    locking._reset()
    settings = _settings(tmp_path, ENABLE_IMMICH=False)
    _caps(respx)
    respx.route(
        method="PROPFIND", url=f"{NC}/remote.php/dav/systemtags-relations/files/50"
    ).mock(return_value=httpx.Response(207, content=_tags_relation((1, "immich"))))

    src = FileRef(fileid=50, path="Photos/a.jpg")
    with NextcloudClient(settings) as client:
        Pipeline(client, settings).process(
            TagEvent(uid=USER, fileids=[50], tagids=[1], files=[src])
        )

    # No call to Immich, no untag (tag wasn't a configured action).
    assert not any(IMM in str(c.request.url) for c in respx.calls)
    assert not any(c.request.method == "DELETE" for c in respx.calls)


# --------------------------------------------------------------------------- #
# poller prefix-tag pickup
# --------------------------------------------------------------------------- #


@respx.mock
def test_poller_picks_up_immich_prefix_tags(tmp_path: Path) -> None:
    locking._reset()
    settings = _settings(tmp_path, TAG_ACTIONS={})  # no fixed tags
    _caps(respx)
    # list_tags returns the immich + immich-<album> tags (+ an unrelated one).
    systemtags = (
        b'<?xml version="1.0"?>'
        b'<d:multistatus xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">'
        b"<d:response><d:href>/remote.php/dav/systemtags/10</d:href><d:propstat><d:prop>"
        b"<oc:id>10</oc:id><oc:display-name>immich</oc:display-name></d:prop>"
        b"<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
        b"<d:response><d:href>/remote.php/dav/systemtags/11</d:href><d:propstat><d:prop>"
        b"<oc:id>11</oc:id><oc:display-name>immich-Trip</oc:display-name></d:prop>"
        b"<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
        b"<d:response><d:href>/remote.php/dav/systemtags/12</d:href><d:propstat><d:prop>"
        b"<oc:id>12</oc:id><oc:display-name>vacation</oc:display-name></d:prop>"
        b"<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
        b"</d:multistatus>"
    )
    respx.route(method="PROPFIND", url=f"{NC}/remote.php/dav/systemtags/").mock(
        return_value=httpx.Response(207, content=systemtags)
    )

    # search_by_tag for tag 10 and 11 -> one file each.
    def _report(fileid: int, path: str) -> bytes:
        return (
            '<?xml version="1.0"?>'
            '<d:multistatus xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">'
            f"<d:response><d:href>/remote.php/dav/files/{USER}/{path}</d:href>"
            f"<d:propstat><d:prop><oc:fileid>{fileid}</oc:fileid><d:resourcetype/></d:prop>"
            "<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response></d:multistatus>"
        ).encode()

    report_route = respx.route(
        method="REPORT", url=f"{NC}/remote.php/dav/files/{USER}/"
    )
    report_route.side_effect = [
        httpx.Response(207, content=_report(101, "Photos/a.jpg")),
        httpx.Response(207, content=_report(102, "Photos/b.jpg")),
    ]

    # The pipeline will then process each — capture the events instead.
    captured: list[TagEvent] = []

    class _CapturePipeline:
        def process(self, event: TagEvent) -> None:
            captured.append(event)

    with NextcloudClient(settings) as client:
        poller = Poller(client, _CapturePipeline(), settings)  # type: ignore[arg-type]
        seen = poller.sweep()

    assert seen == 2
    # tag 10 (immich) -> album None; tag 11 (immich-Trip) -> album "Trip".
    by_tag = {e.tagids[0]: e for e in captured}
    assert by_tag[10].raw.get("immich_album") is None
    assert by_tag[11].raw.get("immich_album") == "Trip"


# --------------------------------------------------------------------------- #
# single-file upload — multipart fields, checksum, no album
# --------------------------------------------------------------------------- #


def _wire_single_file(
    tmp_path: Path,
    *,
    bulk_action: str,
    bulk_asset_id: str | None,
    upload_status: int,
    upload_body: dict[str, object] | None = None,
    settings_over: dict[str, object] | None = None,
) -> tuple[Settings, respx.Route, respx.Route]:
    settings = _settings(tmp_path, **(settings_over or {}))
    _caps(respx)
    _media_types(respx)
    # the file carries the immich tag (tag id 1)
    respx.route(
        method="PROPFIND", url=f"{NC}/remote.php/dav/systemtags-relations/files/77"
    ).mock(return_value=httpx.Response(207, content=_tags_relation((1, "immich"))))
    # download the bytes
    respx.get(f"{NC}/remote.php/dav/files/{USER}/Photos/a.jpg").mock(
        return_value=httpx.Response(200, content=b"\xff\xd8\xff\xe0JFIF-fake-jpeg")
    )
    # mtime PROPFIND
    respx.route(
        method="PROPFIND", url=f"{NC}/remote.php/dav/files/{USER}/Photos/a.jpg"
    ).mock(return_value=httpx.Response(207, content=_lastmodified()))
    # bulk-check
    respx.post(f"{IMM}/api/assets/bulk-upload-check").mock(
        return_value=httpx.Response(
            200,
            json={"results": [{"id": "0", "action": bulk_action, "assetId": bulk_asset_id}]},
        )
    )
    # upload
    body = upload_body or {"id": "asset-xyz", "status": "created"}
    up = respx.post(f"{IMM}/api/assets").mock(
        return_value=httpx.Response(upload_status, json=body)
    )
    untag = respx.delete(
        f"{NC}/remote.php/dav/systemtags-relations/files/77/1"
    ).mock(return_value=httpx.Response(204))
    return settings, up, untag


@respx.mock
def test_single_file_upload_multipart_and_checksum(tmp_path: Path) -> None:
    locking._reset()
    settings, up, untag = _wire_single_file(
        tmp_path, bulk_action="accept", bulk_asset_id=None, upload_status=201
    )

    src = FileRef(fileid=77, path="Photos/a.jpg")
    with NextcloudClient(settings) as client:
        Pipeline(client, settings).process(
            TagEvent(uid=USER, fileids=[77], tagids=[1], files=[src])
        )

    assert up.called
    req = up.calls[0].request
    # multipart body carries assetData + the device + ISO timestamps.
    text = req.content.decode("latin-1")
    assert "assetData" in text
    assert 'name="deviceAssetId"' in text and "nc:77" in text
    assert 'name="deviceId"' in text and "nextcloud-powertools" in text
    assert 'name="fileCreatedAt"' in text and "2025-01-01T12:00:00.000Z" in text
    assert 'name="fileModifiedAt"' in text
    # checksum header present (SHA-1 hex).
    assert "x-immich-checksum" in {k.lower() for k in req.headers}
    csum = req.headers["x-immich-checksum"]
    assert len(csum) == 40  # sha1 hex
    # multipart content-type set by httpx (we never set it manually)
    assert req.headers["content-type"].startswith("multipart/form-data")
    assert untag.call_count == 1  # trigger tag removed on success


@respx.mock
def test_single_file_duplicate_skips_upload_but_adds_to_album(tmp_path: Path) -> None:
    locking._reset()
    # bulk-check rejects as duplicate -> we should NOT POST /api/assets, but still
    # add the harvested existing asset id to the album.
    settings, up, untag = _wire_single_file(
        tmp_path,
        bulk_action="reject",
        bulk_asset_id="existing-123",
        upload_status=201,
        settings_over={},
    )
    # Re-point the tag to immich-Holiday so an album is involved.
    respx.route(
        method="PROPFIND", url=f"{NC}/remote.php/dav/systemtags-relations/files/77"
    ).mock(return_value=httpx.Response(207, content=_tags_relation((1, "immich-Holiday"))))
    # album list -> empty -> create-with-assets
    respx.get(f"{IMM}/api/albums").mock(return_value=httpx.Response(200, json=[]))
    create = respx.post(f"{IMM}/api/albums").mock(
        return_value=httpx.Response(201, json={"id": "album-1", "albumName": "Holiday"})
    )

    src = FileRef(fileid=77, path="Photos/a.jpg")
    with NextcloudClient(settings) as client:
        Pipeline(client, settings).process(
            TagEvent(uid=USER, fileids=[77], tagids=[1], files=[src])
        )

    assert not up.called  # duplicate -> no upload
    assert create.called
    payload = create.calls[0].request.content.decode()
    assert "existing-123" in payload  # harvested duplicate id added to the album
    assert "Holiday" in payload
    assert untag.call_count == 1


# --------------------------------------------------------------------------- #
# album add-to-existing + multiple same-name
# --------------------------------------------------------------------------- #


@respx.mock
def test_album_add_to_existing_oldest_chosen(tmp_path: Path) -> None:
    locking._reset()
    settings, up, untag = _wire_single_file(
        tmp_path, bulk_action="accept", bulk_asset_id=None, upload_status=201,
        upload_body={"id": "new-asset", "status": "created"},
    )
    respx.route(
        method="PROPFIND", url=f"{NC}/remote.php/dav/systemtags-relations/files/77"
    ).mock(return_value=httpx.Response(207, content=_tags_relation((1, "immich-Trip"))))
    # two albums share the name "Trip" -> oldest by createdAt wins (album-old)
    respx.get(f"{IMM}/api/albums").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"id": "album-new", "albumName": "Trip", "createdAt": "2025-06-01T00:00:00Z"},
                {"id": "album-old", "albumName": "Trip", "createdAt": "2024-01-01T00:00:00Z"},
                {"id": "other", "albumName": "Other", "createdAt": "2020-01-01T00:00:00Z"},
            ],
        )
    )
    put = respx.put(f"{IMM}/api/albums/album-old/assets").mock(
        return_value=httpx.Response(200, json=[{"id": "new-asset", "success": True}])
    )

    src = FileRef(fileid=77, path="Photos/a.jpg")
    with NextcloudClient(settings) as client:
        Pipeline(client, settings).process(
            TagEvent(uid=USER, fileids=[77], tagids=[1], files=[src])
        )

    assert up.called
    assert put.called  # added to the OLDEST matching album, not created anew
    assert "new-asset" in put.calls[0].request.content.decode()
    assert untag.call_count == 1


# --------------------------------------------------------------------------- #
# directory walk: media-types filter + MAX_FILES + all added to album
# --------------------------------------------------------------------------- #


def _dir_zip(files: dict[str, bytes]) -> bytes:
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


@respx.mock
def test_directory_only_media_uploaded_and_added_to_album(tmp_path: Path) -> None:
    locking._reset()
    settings = _settings(tmp_path)
    _caps(respx)
    _media_types(respx)
    respx.route(
        method="PROPFIND", url=f"{NC}/remote.php/dav/systemtags-relations/files/90"
    ).mock(return_value=httpx.Response(207, content=_tags_relation((3, "immich-Album1"))))
    # download the folder as a zip: a.jpg + b.mp4 (media) + notes.txt (skipped)
    zip_bytes = _dir_zip(
        {"a.jpg": b"img-a", "sub/b.mp4": b"vid-b", "notes.txt": b"text"}
    )
    respx.get(f"{NC}/remote.php/dav/files/{USER}/Trip").mock(
        return_value=httpx.Response(200, content=zip_bytes)
    )
    # mtime for each media file
    respx.route(
        method="PROPFIND", url__regex=rf"{NC}/remote.php/dav/files/{USER}/Trip/.*"
    ).mock(return_value=httpx.Response(207, content=_lastmodified()))
    # bulk-check: both accepted
    respx.post(f"{IMM}/api/assets/bulk-upload-check").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {"id": "0", "action": "accept"},
                    {"id": "1", "action": "accept"},
                ]
            },
        )
    )
    # uploads return distinct asset ids
    upload_route = respx.post(f"{IMM}/api/assets")
    upload_route.side_effect = [
        httpx.Response(201, json={"id": "asset-a", "status": "created"}),
        httpx.Response(201, json={"id": "asset-b", "status": "created"}),
    ]
    respx.get(f"{IMM}/api/albums").mock(return_value=httpx.Response(200, json=[]))
    create = respx.post(f"{IMM}/api/albums").mock(
        return_value=httpx.Response(201, json={"id": "alb", "albumName": "Album1"})
    )
    untag = respx.delete(
        f"{NC}/remote.php/dav/systemtags-relations/files/90/3"
    ).mock(return_value=httpx.Response(204))

    src = FileRef(fileid=90, path="Trip", is_dir=True)
    with NextcloudClient(settings) as client:
        Pipeline(client, settings).process(
            TagEvent(uid=USER, fileids=[90], tagids=[3], files=[src])
        )

    # exactly 2 uploads (jpg + mp4); txt skipped by the media-types filter.
    assert upload_route.call_count == 2
    assert create.called
    body = create.calls[0].request.content.decode()
    assert "asset-a" in body and "asset-b" in body
    assert untag.call_count == 1


@respx.mock
def test_directory_max_files_cap(tmp_path: Path) -> None:
    locking._reset()
    settings = _settings(tmp_path, MAX_FILES=1, ERROR_TAG="")
    _caps(respx)
    _media_types(respx)
    respx.route(
        method="PROPFIND", url=f"{NC}/remote.php/dav/systemtags-relations/files/91"
    ).mock(return_value=httpx.Response(207, content=_tags_relation((3, "immich"))))
    zip_bytes = _dir_zip({"a.jpg": b"a", "b.jpg": b"b"})  # 2 media > cap 1
    respx.get(f"{NC}/remote.php/dav/files/{USER}/Trip").mock(
        return_value=httpx.Response(200, content=zip_bytes)
    )
    untag = respx.delete(f"{NC}/remote.php/dav/systemtags-relations/files/91/3")

    src = FileRef(fileid=91, path="Trip", is_dir=True)
    with NextcloudClient(settings) as client:
        Pipeline(client, settings).process(
            TagEvent(uid=USER, fileids=[91], tagids=[3], files=[src])
        )

    # cap exceeded -> HandlerError -> no upload, trigger tag KEPT (not removed)
    assert not any(c.request.url.path == "/api/assets" for c in respx.calls)
    assert not untag.called


# --------------------------------------------------------------------------- #
# failure: 500 on upload -> trigger tag kept + ERROR_TAG
# --------------------------------------------------------------------------- #


@respx.mock
def test_upload_500_keeps_trigger_tag_and_sets_error_tag(tmp_path: Path) -> None:
    locking._reset()
    settings = _settings(tmp_path, ERROR_TAG="powertools-error")
    _caps(respx)
    _media_types(respx)
    respx.route(
        method="PROPFIND", url=f"{NC}/remote.php/dav/systemtags-relations/files/77"
    ).mock(return_value=httpx.Response(207, content=_tags_relation((1, "immich"))))
    respx.get(f"{NC}/remote.php/dav/files/{USER}/Photos/a.jpg").mock(
        return_value=httpx.Response(200, content=b"jpeg-bytes")
    )
    respx.route(
        method="PROPFIND", url=f"{NC}/remote.php/dav/files/{USER}/Photos/a.jpg"
    ).mock(return_value=httpx.Response(207, content=_lastmodified()))
    respx.post(f"{IMM}/api/assets/bulk-upload-check").mock(
        return_value=httpx.Response(200, json={"results": [{"id": "0", "action": "accept"}]})
    )
    respx.post(f"{IMM}/api/assets").mock(return_value=httpx.Response(500, text="boom"))
    untag = respx.delete(f"{NC}/remote.php/dav/systemtags-relations/files/77/1")
    # ERROR_TAG path: ensure_tag (list systemtags) + assign relation
    respx.route(method="PROPFIND", url=f"{NC}/remote.php/dav/systemtags/").mock(
        return_value=httpx.Response(
            207,
            content=(
                b'<?xml version="1.0"?>'
                b'<d:multistatus xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">'
                b"<d:response><d:href>/remote.php/dav/systemtags/9</d:href><d:propstat>"
                b"<d:prop><oc:id>9</oc:id><oc:display-name>powertools-error</oc:display-name>"
                b"</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
                b"</d:multistatus>"
            ),
        )
    )
    assign = respx.put(
        f"{NC}/remote.php/dav/systemtags-relations/files/77/9"
    ).mock(return_value=httpx.Response(201))

    src = FileRef(fileid=77, path="Photos/a.jpg")
    with NextcloudClient(settings) as client:
        Pipeline(client, settings).process(
            TagEvent(uid=USER, fileids=[77], tagids=[1], files=[src])
        )

    assert not untag.called  # trigger tag KEPT (retriable)
    assert assign.called  # ERROR_TAG assigned


# --------------------------------------------------------------------------- #
# service-level: media_types fallback + sha1 helper
# --------------------------------------------------------------------------- #


@respx.mock
def test_media_types_fallback_when_unreachable(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    respx.get(f"{IMM}/api/server/media-types").mock(side_effect=httpx.ConnectError("down"))
    with ImmichService(settings) as immich:
        mt = immich.media_types()
        assert "jpg" in mt["image"]
        assert "mp4" in mt["video"]
        # accepted-media uses the fallback set
        assert immich.is_accepted_media("x.JPG")
        assert not immich.is_accepted_media("x.txt")


def test_sha1_of_file(tmp_path: Path) -> None:
    import hashlib

    p = tmp_path / "f.bin"
    p.write_bytes(b"hello immich")
    assert sha1_of_file(p) == hashlib.sha1(b"hello immich").hexdigest()  # noqa: S324


@respx.mock
def test_immich_ping_and_version(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    respx.get(f"{IMM}/api/server/ping").mock(
        return_value=httpx.Response(200, json={"res": "pong"})
    )
    respx.get(f"{IMM}/api/server/version").mock(
        return_value=httpx.Response(200, json={"major": 2, "minor": 7, "patch": 5})
    )
    with ImmichService(settings) as immich:
        assert immich.ping() is True
        assert immich.version() == "2.7.5"


def test_tagspec_unused_import_guard() -> None:
    # TagSpec is imported for parity with other test modules; assert it constructs.
    assert TagSpec(id=1, name="immich").name == "immich"

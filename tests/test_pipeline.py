"""Pipeline orchestration tests (respx-mocked NC).

Covers the M3 DEMO.md bullets:
* success flow: upload targets the PARENT dir, trigger-tag DELETE issued exactly
  once, NO DELETE on the original file/folder;
* extract multi-file output recreates the subfolder tree under the parent;
* failure path: trigger tag kept, ERROR_TAG assigned, temp cleaned, notify when
  enabled;
* lock: concurrent same-fileid events run the handler once.
"""

from __future__ import annotations

import io
import threading
import zipfile
from pathlib import Path

import httpx
import respx

from ncpowertools import locking
from ncpowertools.config import Settings
from ncpowertools.models import TagEvent
from ncpowertools.nextcloud import NextcloudClient
from ncpowertools.pipeline import Pipeline

BASE = "https://cloud.example.com"
USER = "powertools"


def _settings(tmp_path: Path, **over: object) -> Settings:
    kw: dict[str, object] = {
        "NEXTCLOUD_URL": BASE,
        "NC_USER": USER,
        "NC_APP_PASSWORD": "app-pw",
        "WORK_DIR": str(tmp_path / "work"),
        "ERROR_TAG": "powertools-error",
        "NOTIFY": False,
        "POLL_INTERVAL": 0,
    }
    kw.update(over)
    return Settings(**kw)  # type: ignore[arg-type]


def _file_report(fileid: int, path: str, is_dir: bool = False) -> bytes:
    rt = "<d:resourcetype><d:collection/></d:resourcetype>" if is_dir else "<d:resourcetype/>"
    href = f"/remote.php/dav/files/{USER}/{path}"
    if is_dir and not href.endswith("/"):
        href += "/"
    return (
        '<?xml version="1.0"?>'
        '<d:multistatus xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">'
        f"<d:response><d:href>{href}</d:href>"
        f"<d:propstat><d:prop><oc:fileid>{fileid}</oc:fileid>{rt}</d:prop>"
        "<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
        "</d:multistatus>"
    ).encode()


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


def _caps(mock: respx.MockRouter, major: int = 33) -> None:
    mock.get(f"{BASE}/ocs/v2.php/cloud/capabilities").mock(
        return_value=httpx.Response(
            200, json={"ocs": {"data": {"version": {"major": major, "minor": 0, "micro": 0}}}}
        )
    )


def _zip_bytes(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# success: zip a file -> upload to parent, remove trigger tag, no DELETE on src
# --------------------------------------------------------------------------- #


@respx.mock
def test_zip_success_uploads_to_parent_and_untags(tmp_path: Path) -> None:
    locking._reset()
    settings = _settings(tmp_path)
    _caps(respx)
    # resolve fileid -> Docs/report.txt
    respx.route(method="REPORT", url=f"{BASE}/remote.php/dav/files/{USER}/").mock(
        return_value=httpx.Response(207, content=_file_report(42, "Docs/report.txt"))
    )
    # tags on file -> tagid 9 = "zip"
    respx.route(
        method="PROPFIND", url=f"{BASE}/remote.php/dav/systemtags-relations/files/42"
    ).mock(return_value=httpx.Response(207, content=_tags_relation((9, "zip"))))
    # download the source file
    respx.get(f"{BASE}/remote.php/dav/files/{USER}/Docs/report.txt").mock(
        return_value=httpx.Response(200, content=b"hello world")
    )
    # ensure_dir (AutoMkcol on 33) + upload to parent
    respx.route(method="MKCOL").mock(return_value=httpx.Response(405))
    upload = respx.put(f"{BASE}/remote.php/dav/files/{USER}/Docs/report.txt.zip").mock(
        return_value=httpx.Response(201)
    )
    untag = respx.delete(
        f"{BASE}/remote.php/dav/systemtags-relations/files/42/9"
    ).mock(return_value=httpx.Response(204))

    with NextcloudClient(settings) as client:
        Pipeline(client, settings).process(
            TagEvent(uid=USER, fileids=[42], tagids=[9])
        )

    assert upload.called
    # upload target is in the SOURCE's parent ("Docs/")
    assert upload.calls[0].request.url.path.endswith("/Docs/report.txt.zip")
    # trigger-tag relation DELETE issued exactly once
    assert untag.call_count == 1
    # NO DELETE was issued against any files/ path (user content)
    for call in respx.calls:
        if call.request.method == "DELETE":
            assert "/systemtags-relations/" in call.request.url.path
    # temp cleaned
    assert not (Path(settings.WORK_DIR) / "42").exists()


# --------------------------------------------------------------------------- #
# extract: multi-file output recreates the subfolder tree under the parent
# --------------------------------------------------------------------------- #


@respx.mock
def test_extract_recreates_subfolder_tree(tmp_path: Path) -> None:
    locking._reset()
    settings = _settings(tmp_path)
    _caps(respx)
    respx.route(method="REPORT", url=f"{BASE}/remote.php/dav/files/{USER}/").mock(
        return_value=httpx.Response(207, content=_file_report(7, "Inbox/bundle.zip"))
    )
    respx.route(
        method="PROPFIND", url=f"{BASE}/remote.php/dav/systemtags-relations/files/7"
    ).mock(return_value=httpx.Response(207, content=_tags_relation((3, "extract"))))
    archive = _zip_bytes({"a/b.txt": b"deep", "top.txt": b"top"})
    respx.get(f"{BASE}/remote.php/dav/files/{USER}/Inbox/bundle.zip").mock(
        return_value=httpx.Response(200, content=archive)
    )
    respx.route(method="MKCOL").mock(return_value=httpx.Response(201))
    uploads = respx.put(url__regex=rf"{BASE}/remote.php/dav/files/{USER}/Inbox/bundle/.*").mock(
        return_value=httpx.Response(201)
    )
    respx.delete(f"{BASE}/remote.php/dav/systemtags-relations/files/7/3").mock(
        return_value=httpx.Response(204)
    )

    with NextcloudClient(settings) as client:
        Pipeline(client, settings).process(TagEvent(uid=USER, fileids=[7], tagids=[3]))

    targets = {c.request.url.path for c in uploads.calls}
    assert any(p.endswith("/Inbox/bundle/a/b.txt") for p in targets)
    assert any(p.endswith("/Inbox/bundle/top.txt") for p in targets)
    # the nested subfolder was created via MKCOL/AutoMkcol
    mkcols = [c for c in respx.calls if c.request.method == "MKCOL"]
    assert any("Inbox/bundle/a" in c.request.url.path for c in mkcols)


# --------------------------------------------------------------------------- #
# failure path: keep trigger tag, assign ERROR_TAG, clean temp, notify
# --------------------------------------------------------------------------- #


@respx.mock
def test_failure_keeps_tag_assigns_error_and_notifies(tmp_path: Path) -> None:
    locking._reset()
    settings = _settings(tmp_path, NOTIFY=True)
    _caps(respx)
    respx.route(method="REPORT", url=f"{BASE}/remote.php/dav/files/{USER}/").mock(
        return_value=httpx.Response(207, content=_file_report(5, "Inbox/broken.zip"))
    )
    respx.route(
        method="PROPFIND", url=f"{BASE}/remote.php/dav/systemtags-relations/files/5"
    ).mock(return_value=httpx.Response(207, content=_tags_relation((3, "extract"))))
    # download returns a corrupt zip -> handler raises HandlerError
    respx.get(f"{BASE}/remote.php/dav/files/{USER}/Inbox/broken.zip").mock(
        return_value=httpx.Response(200, content=b"not a zip")
    )
    # ensure_tag(ERROR_TAG): list returns no such tag -> POST creates it
    respx.route(method="PROPFIND", url=f"{BASE}/remote.php/dav/systemtags/").mock(
        return_value=httpx.Response(
            207,
            content=(
                b'<?xml version="1.0"?>'
                b'<d:multistatus xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns"></d:multistatus>'
            ),
        )
    )
    respx.post(f"{BASE}/remote.php/dav/systemtags/").mock(
        return_value=httpx.Response(
            201, headers={"Content-Location": "/remote.php/dav/systemtags/99"}
        )
    )
    assign = respx.put(
        f"{BASE}/remote.php/dav/systemtags-relations/files/5/99"
    ).mock(return_value=httpx.Response(201))
    untag = respx.delete(f"{BASE}/remote.php/dav/systemtags-relations/files/5/3").mock(
        return_value=httpx.Response(204)
    )
    notify = respx.post(
        url__regex=rf"{BASE}/ocs/v2.php/apps/notifications/.*"
    ).mock(return_value=httpx.Response(200, json={"ocs": {"meta": {"status": "ok"}}}))

    with NextcloudClient(settings) as client:
        Pipeline(client, settings).process(TagEvent(uid=USER, fileids=[5], tagids=[3]))

    assert assign.call_count == 1  # ERROR_TAG assigned
    assert untag.call_count == 0  # trigger tag NOT removed (retriable)
    assert notify.called  # failure notification sent
    assert not (Path(settings.WORK_DIR) / "5").exists()  # temp cleaned


# --------------------------------------------------------------------------- #
# folder + render-png (F1): download subtree, walk+render, upload to mirrored
# paths INSIDE the tagged dir, untag, never DELETE / re-upload originals
# --------------------------------------------------------------------------- #


@respx.mock
def test_render_png_on_folder_renders_tree(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    locking._reset()
    settings = _settings(tmp_path)
    _caps(respx)
    # Pretend ImageMagick is present and render by writing a tiny PNG to argv[-1].
    import subprocess as _subprocess

    from ncpowertools.handlers import render as render_mod

    monkeypatch.setattr(
        render_mod.shutil,
        "which",
        lambda name: "/usr/bin/convert" if name in ("magick", "convert") else None,
    )

    def fake_run(argv, **kwargs):  # noqa: ANN001, ANN003, ANN202
        Path(argv[-1]).write_bytes(b"\x89PNG\r\n\x1a\n")
        return _subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(render_mod.subprocess, "run", fake_run)

    respx.route(method="REPORT", url=f"{BASE}/remote.php/dav/files/{USER}/").mock(
        return_value=httpx.Response(207, content=_file_report(8, "Album", is_dir=True))
    )
    respx.route(
        method="PROPFIND", url=f"{BASE}/remote.php/dav/systemtags-relations/files/8"
    ).mock(return_value=httpx.Response(207, content=_tags_relation((4, "render-png"))))
    # directory GET returns a zip of the folder: a.psd, sub/b.psd, notes.txt
    respx.get(f"{BASE}/remote.php/dav/files/{USER}/Album").mock(
        return_value=httpx.Response(
            200,
            content=_zip_bytes(
                {"a.psd": b"8BPS", "sub/b.psd": b"8BPS", "notes.txt": b"hi"}
            ),
        )
    )
    respx.route(method="MKCOL").mock(return_value=httpx.Response(201))
    uploads = respx.put(
        url__regex=rf"{BASE}/remote.php/dav/files/{USER}/Album/.*"
    ).mock(return_value=httpx.Response(201))
    untag = respx.delete(
        f"{BASE}/remote.php/dav/systemtags-relations/files/8/4"
    ).mock(return_value=httpx.Response(204))

    with NextcloudClient(settings) as client:
        Pipeline(client, settings).process(TagEvent(uid=USER, fileids=[8], tagids=[4]))

    targets = {c.request.url.path for c in uploads.calls}
    # outputs land beside each source, INSIDE the tagged dir, tree mirrored
    assert any(p.endswith("/Album/a.png") for p in targets)
    assert any(p.endswith("/Album/sub/b.png") for p in targets)
    # exactly the two renderable files were uploaded — nothing else
    assert len(targets) == 2
    # the non-renderable notes.txt was NOT re-uploaded
    assert not any(p.endswith("notes.txt") for p in targets)
    # the originals (.psd) were never re-uploaded
    assert not any(p.endswith(".psd") for p in targets)
    # trigger tag removed; no DELETE on user content
    assert untag.call_count == 1
    for call in respx.calls:
        if call.request.method == "DELETE":
            assert "/systemtags-relations/" in call.request.url.path


@respx.mock
def test_render_on_empty_folder_untags_uploads_nothing(tmp_path: Path) -> None:
    """A folder with no renderable files → success: untag, no upload, no error."""
    locking._reset()
    settings = _settings(tmp_path)
    _caps(respx)
    respx.route(method="REPORT", url=f"{BASE}/remote.php/dav/files/{USER}/").mock(
        return_value=httpx.Response(207, content=_file_report(8, "Album", is_dir=True))
    )
    respx.route(
        method="PROPFIND", url=f"{BASE}/remote.php/dav/systemtags-relations/files/8"
    ).mock(return_value=httpx.Response(207, content=_tags_relation((4, "render-png"))))
    respx.get(f"{BASE}/remote.php/dav/files/{USER}/Album").mock(
        return_value=httpx.Response(200, content=_zip_bytes({"notes.txt": b"hi"}))
    )
    untag = respx.delete(
        f"{BASE}/remote.php/dav/systemtags-relations/files/8/4"
    ).mock(return_value=httpx.Response(204))

    with NextcloudClient(settings) as client:
        Pipeline(client, settings).process(TagEvent(uid=USER, fileids=[8], tagids=[4]))

    # nothing rendered → no PUT, but the trigger tag IS removed (treated success)
    assert not any(c.request.method == "PUT" for c in respx.calls)
    assert untag.call_count == 1


@respx.mock
def test_render_png_single_file_still_works(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    """Single-FILE render is unchanged: output beside the source in its parent."""
    locking._reset()
    settings = _settings(tmp_path)
    _caps(respx)
    import subprocess as _subprocess

    from ncpowertools.handlers import render as render_mod

    monkeypatch.setattr(
        render_mod.shutil,
        "which",
        lambda name: "/usr/bin/convert" if name in ("magick", "convert") else None,
    )

    def fake_run(argv, **kwargs):  # noqa: ANN001, ANN003, ANN202
        Path(argv[-1]).write_bytes(b"\x89PNG\r\n\x1a\n")
        return _subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(render_mod.subprocess, "run", fake_run)

    respx.route(method="REPORT", url=f"{BASE}/remote.php/dav/files/{USER}/").mock(
        return_value=httpx.Response(207, content=_file_report(12, "Album/art.psd"))
    )
    respx.route(
        method="PROPFIND", url=f"{BASE}/remote.php/dav/systemtags-relations/files/12"
    ).mock(return_value=httpx.Response(207, content=_tags_relation((4, "render-png"))))
    respx.get(f"{BASE}/remote.php/dav/files/{USER}/Album/art.psd").mock(
        return_value=httpx.Response(200, content=b"8BPS")
    )
    respx.route(method="MKCOL").mock(return_value=httpx.Response(201))
    upload = respx.put(
        f"{BASE}/remote.php/dav/files/{USER}/Album/art.png"
    ).mock(return_value=httpx.Response(201))
    untag = respx.delete(
        f"{BASE}/remote.php/dav/systemtags-relations/files/12/4"
    ).mock(return_value=httpx.Response(204))

    with NextcloudClient(settings) as client:
        Pipeline(client, settings).process(TagEvent(uid=USER, fileids=[12], tagids=[4]))

    assert upload.called
    assert upload.calls[0].request.url.path.endswith("/Album/art.png")
    assert untag.call_count == 1


# --------------------------------------------------------------------------- #
# folder + zip: download-as-archive, unpack, recompress, upload to parent
# --------------------------------------------------------------------------- #


@respx.mock
def test_zip_folder_downloads_archive_and_uploads(tmp_path: Path) -> None:
    locking._reset()
    settings = _settings(tmp_path)
    _caps(respx)
    respx.route(method="REPORT", url=f"{BASE}/remote.php/dav/files/{USER}/").mock(
        return_value=httpx.Response(207, content=_file_report(11, "Work/proj", is_dir=True))
    )
    respx.route(
        method="PROPFIND", url=f"{BASE}/remote.php/dav/systemtags-relations/files/11"
    ).mock(return_value=httpx.Response(207, content=_tags_relation((9, "zip"))))
    # directory GET returns a zip of the folder contents
    respx.get(f"{BASE}/remote.php/dav/files/{USER}/Work/proj").mock(
        return_value=httpx.Response(200, content=_zip_bytes({"x.txt": b"1", "sub/y.txt": b"2"}))
    )
    respx.route(method="MKCOL").mock(return_value=httpx.Response(405))
    upload = respx.put(f"{BASE}/remote.php/dav/files/{USER}/Work/proj.zip").mock(
        return_value=httpx.Response(201)
    )
    respx.delete(f"{BASE}/remote.php/dav/systemtags-relations/files/11/9").mock(
        return_value=httpx.Response(204)
    )

    with NextcloudClient(settings) as client:
        Pipeline(client, settings).process(TagEvent(uid=USER, fileids=[11], tagids=[9]))

    assert upload.called
    assert upload.calls[0].request.url.path.endswith("/Work/proj.zip")


# --------------------------------------------------------------------------- #
# lock: concurrent same-fileid events run the handler once
# --------------------------------------------------------------------------- #


def test_lock_runs_handler_once_for_concurrent_events(tmp_path: Path) -> None:
    locking._reset()
    settings = _settings(tmp_path)

    runs: list[int] = []
    gate = threading.Event()

    class FakeClient:
        def resolve_fileid(self, fileid: int, user: str | None = None):  # noqa: ANN001
            from ncpowertools.models import FileRef

            return FileRef(fileid=fileid, path="Docs/f.txt")

        def tags_on_file(self, fileid: int):  # noqa: ANN001
            from ncpowertools.models import TagSpec

            return [TagSpec(id=9, name="zip")]

        def download_to(self, path: str, dest: Path) -> Path:
            runs.append(1)
            gate.wait(2.0)  # hold the lock so the 2nd thread races
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"x")
            return dest

        def ensure_dir(self, path: str) -> None: ...
        def upload(self, path: str, data: object) -> None: ...
        def remove_tag(self, fileid: int, tagid: int) -> None: ...

    pipeline = Pipeline(FakeClient(), settings)  # type: ignore[arg-type]
    ev = TagEvent(uid=USER, fileids=[77], tagids=[9])

    t1 = threading.Thread(target=pipeline.process, args=(ev,))
    t2 = threading.Thread(target=pipeline.process, args=(ev,))
    t1.start()
    # ensure t1 grabs the lock first
    while not locking.is_processing(77):
        pass
    t2.start()
    gate.set()
    t1.join(3.0)
    t2.join(3.0)

    assert runs == [1]  # handler ran exactly once

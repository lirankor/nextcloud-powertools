"""Smoke test: import the package and run `selftest` against a fully mocked NC."""

from __future__ import annotations

import httpx
import respx

import ncpowertools
from ncpowertools.cli import main

BASE = "https://cloud.example.com"
USER = "powertools"

SYSTEMTAGS_XML = (
    '<?xml version="1.0"?>'
    '<d:multistatus xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">'
    "<d:response><d:href>/remote.php/dav/systemtags/1</d:href>"
    "<d:propstat><d:prop><oc:id>1</oc:id><oc:display-name>extract</oc:display-name>"
    "</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
    "<d:response><d:href>/remote.php/dav/systemtags/2</d:href>"
    "<d:propstat><d:prop><oc:id>2</oc:id><oc:display-name>zip</oc:display-name>"
    "</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
    "<d:response><d:href>/remote.php/dav/systemtags/3</d:href>"
    "<d:propstat><d:prop><oc:id>3</oc:id><oc:display-name>rar</oc:display-name>"
    "</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
    "<d:response><d:href>/remote.php/dav/systemtags/4</d:href>"
    "<d:propstat><d:prop><oc:id>4</oc:id><oc:display-name>render-png</oc:display-name>"
    "</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
    "<d:response><d:href>/remote.php/dav/systemtags/5</d:href>"
    "<d:propstat><d:prop><oc:id>5</oc:id><oc:display-name>render</oc:display-name>"
    "</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
    "</d:multistatus>"
)


def test_package_imports() -> None:
    assert ncpowertools.__version__


def test_help_lists_subcommands(capsys) -> None:
    rc = main([])
    out = capsys.readouterr().out
    assert rc == 0
    for cmd in ("run", "poll-once", "selftest", "list-tags"):
        assert cmd in out


@respx.mock
def test_selftest_green_against_mocked_nc(monkeypatch, capsys) -> None:
    for var, val in {
        "NEXTCLOUD_URL": BASE,
        "NC_USER": USER,
        "NC_APP_PASSWORD": "app-pw",
    }.items():
        monkeypatch.setenv(var, val)
    monkeypatch.setattr("ncpowertools.config.Settings.model_config", {"env_file": None})

    respx.get(f"{BASE}/ocs/v2.php/cloud/capabilities").mock(
        return_value=httpx.Response(
            200, json={"ocs": {"data": {"version": {"major": 33, "minor": 0, "micro": 1}}}}
        )
    )
    respx.route(method="PROPFIND", url=f"{BASE}/remote.php/dav/systemtags/").mock(
        return_value=httpx.Response(207, content=SYSTEMTAGS_XML.encode())
    )

    rc = main(["selftest"])
    out = capsys.readouterr().out
    assert "version 33.0.1" in out
    assert "PASS" in out
    # All five default trigger tags already exist in the mock.
    for tag in ("extract", "zip", "rar", "render-png", "render"):
        assert f"trigger tag '{tag}' exists" in out
    assert rc == 0


@respx.mock
def test_selftest_reports_nc_failure_separately(monkeypatch, capsys) -> None:
    for var, val in {
        "NEXTCLOUD_URL": BASE,
        "NC_USER": USER,
        "NC_APP_PASSWORD": "app-pw",
    }.items():
        monkeypatch.setenv(var, val)
    monkeypatch.setattr("ncpowertools.config.Settings.model_config", {"env_file": None})

    respx.get(f"{BASE}/ocs/v2.php/cloud/capabilities").mock(
        return_value=httpx.Response(503, text="down")
    )

    rc = main(["selftest"])
    out = capsys.readouterr().out
    # Tool phase still ran (separate from NC phase).
    assert "Tools:" in out
    assert "Nextcloud check failed" in out
    assert rc == 1


def _file_report(fileid: int, path: str) -> bytes:
    return (
        '<?xml version="1.0"?>'
        '<d:multistatus xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">'
        f"<d:response><d:href>/remote.php/dav/files/{USER}/{path}</d:href>"
        f"<d:propstat><d:prop><oc:fileid>{fileid}</oc:fileid><d:resourcetype/></d:prop>"
        "<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
        "</d:multistatus>"
    ).encode()


def _empty_report() -> bytes:
    return (
        b'<?xml version="1.0"?>'
        b'<d:multistatus xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns"></d:multistatus>'
    )


@respx.mock
def test_poll_once_sweeps_and_processes(monkeypatch, capsys) -> None:
    """poll-once resolves each trigger tag, searches, and processes a hit."""
    for var, val in {
        "NEXTCLOUD_URL": BASE,
        "NC_USER": USER,
        "NC_APP_PASSWORD": "app-pw",
        "TAG_ACTIONS": '{"extract":"extract"}',  # one tag to keep the mock small
        "POLL_INTERVAL": "0",
        "ERROR_TAG": "",
    }.items():
        monkeypatch.setenv(var, val)
    monkeypatch.setattr("ncpowertools.config.Settings.model_config", {"env_file": None})

    respx.get(f"{BASE}/ocs/v2.php/cloud/capabilities").mock(
        return_value=httpx.Response(
            200, json={"ocs": {"data": {"version": {"major": 33, "minor": 0, "micro": 0}}}}
        )
    )
    # ensure_tag("extract") -> list returns extract=3
    respx.route(method="PROPFIND", url=f"{BASE}/remote.php/dav/systemtags/").mock(
        return_value=httpx.Response(207, content=SYSTEMTAGS_XML.encode())
    )
    # search_by_tag(3) -> one zip file with fileid 50 (REPORT on user root, body
    # differs from resolve but same URL; sequence the two REPORTs by side_effect)
    report_url = respx.route(method="REPORT", url=f"{BASE}/remote.php/dav/files/{USER}/")
    report_url.mock(
        side_effect=[
            httpx.Response(207, content=_file_report(50, "Inbox/a.zip")),  # search_by_tag
            httpx.Response(207, content=_file_report(50, "Inbox/a.zip")),  # resolve_fileid
        ]
    )
    # tags on file 50 -> extract (id 1 per SYSTEMTAGS_XML)
    respx.route(
        method="PROPFIND", url=f"{BASE}/remote.php/dav/systemtags-relations/files/50"
    ).mock(
        return_value=httpx.Response(
            207,
            content=(
                b'<?xml version="1.0"?>'
                b'<d:multistatus xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">'
                b"<d:response><d:href>x</d:href><d:propstat><d:prop>"
                b"<oc:id>1</oc:id><oc:display-name>extract</oc:display-name>"
                b"</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
                b"</d:multistatus>"
            ),
        )
    )
    # download the (empty/corrupt) archive -> handler fails, but that's fine; the
    # point is poll-once swept + dispatched. ERROR_TAG disabled so no extra calls.
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("hello.txt", b"hi")
    respx.get(f"{BASE}/remote.php/dav/files/{USER}/Inbox/a.zip").mock(
        return_value=httpx.Response(200, content=buf.getvalue())
    )
    respx.route(method="MKCOL").mock(return_value=httpx.Response(405))
    respx.put(url__regex=rf"{BASE}/remote.php/dav/files/{USER}/Inbox/a/.*").mock(
        return_value=httpx.Response(201)
    )
    respx.delete(url__regex=rf"{BASE}/remote.php/dav/systemtags-relations/files/50/1").mock(
        return_value=httpx.Response(204)
    )

    rc = main(["poll-once"])
    assert rc == 0
    # the extracted file was uploaded into the parent (Inbox/a/hello.txt)
    assert any(
        c.request.method == "PUT" and c.request.url.path.endswith("/Inbox/a/hello.txt")
        for c in respx.calls
    )

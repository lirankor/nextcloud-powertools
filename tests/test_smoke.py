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

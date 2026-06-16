"""respx-mocked tests for NextcloudClient + webdav_xml builders/parsers.

Asserts exact method+URL for each call and that REPORT/PROPFIND bodies carry
the right filter elements, and that sample multistatus XML parses correctly.
"""

from __future__ import annotations

import base64

import httpx
import pytest
import respx

from ncpowertools.config import Settings
from ncpowertools.errors import NcApiError
from ncpowertools.nextcloud import NextcloudClient
from ncpowertools.nextcloud import webdav_xml as xml

BASE = "https://cloud.example.com"
USER = "powertools"

# --------------------------------------------------------------------------- #
# Sample multistatus XML fixtures (shapes per CONTEXT.md)
# --------------------------------------------------------------------------- #

FILE_REPORT_XML = """<?xml version="1.0"?>
<d:multistatus xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns" xmlns:nc="http://nextcloud.org/ns">
  <d:response>
    <d:href>/remote.php/dav/files/powertools/Documents/My%20Archive.zip</d:href>
    <d:propstat>
      <d:prop>
        <oc:fileid>12345</oc:fileid>
        <d:getcontenttype>application/zip</d:getcontenttype>
        <d:resourcetype/>
      </d:prop>
      <d:status>HTTP/1.1 200 OK</d:status>
    </d:propstat>
  </d:response>
</d:multistatus>"""

FOLDER_REPORT_XML = """<?xml version="1.0"?>
<d:multistatus xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns" xmlns:nc="http://nextcloud.org/ns">
  <d:response>
    <d:href>/remote.php/dav/files/powertools/Photos/</d:href>
    <d:propstat>
      <d:prop>
        <oc:fileid>777</oc:fileid>
        <d:resourcetype><d:collection/></d:resourcetype>
      </d:prop>
      <d:status>HTTP/1.1 200 OK</d:status>
    </d:propstat>
  </d:response>
</d:multistatus>"""

SYSTEMTAGS_XML = """<?xml version="1.0"?>
<d:multistatus xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns" xmlns:nc="http://nextcloud.org/ns">
  <d:response>
    <d:href>/remote.php/dav/systemtags/</d:href>
    <d:propstat><d:prop/><d:status>HTTP/1.1 200 OK</d:status></d:propstat>
  </d:response>
  <d:response>
    <d:href>/remote.php/dav/systemtags/7</d:href>
    <d:propstat>
      <d:prop>
        <oc:id>7</oc:id>
        <oc:display-name>extract</oc:display-name>
        <oc:user-visible>true</oc:user-visible>
        <oc:user-assignable>true</oc:user-assignable>
      </d:prop>
      <d:status>HTTP/1.1 200 OK</d:status>
    </d:propstat>
  </d:response>
  <d:response>
    <d:href>/remote.php/dav/systemtags/9</d:href>
    <d:propstat>
      <d:prop>
        <oc:id>9</oc:id>
        <oc:display-name>zip</oc:display-name>
      </d:prop>
      <d:status>HTTP/1.1 200 OK</d:status>
    </d:propstat>
  </d:response>
</d:multistatus>"""


def _settings() -> Settings:
    return Settings(NEXTCLOUD_URL=BASE, NC_USER=USER, NC_APP_PASSWORD="app-pw")


def _capabilities_route(respx_mock: respx.MockRouter, major: int = 33) -> None:
    respx_mock.get(f"{BASE}/ocs/v2.php/cloud/capabilities").mock(
        return_value=httpx.Response(
            200,
            json={
                "ocs": {
                    "data": {
                        "version": {
                            "major": major,
                            "minor": 0,
                            "micro": 2,
                            "string": f"{major}.0.2",
                        }
                    }
                }
            },
        )
    )


# --------------------------------------------------------------------------- #
# webdav_xml unit tests (pure functions)
# --------------------------------------------------------------------------- #


def test_build_fileid_search_is_search_request_on_user_scope() -> None:
    body = xml.build_fileid_search(12345, USER).decode()
    # SEARCH/basicsearch — the supported NC fileid resolver (NOT filter-files).
    assert "d:searchrequest" in body
    assert "d:basicsearch" in body
    # scoped to the user's own files tree at infinite depth
    assert f"<d:href>/files/{USER}</d:href>" in body
    assert "<d:depth>infinity</d:depth>" in body
    # WHERE oc:fileid == literal, and resourcetype selected so is_dir is known
    assert "<oc:fileid/>" in body
    assert "<d:literal>12345</d:literal>" in body
    assert "d:resourcetype" in body
    # must NOT be the broken oc:filter-files / fileid filter-rule shape
    assert "filter-files" not in body
    assert "filter-rules" not in body


def test_build_fileid_report_is_deprecated_regression_guard() -> None:
    # The old (broken) builder still exists ONLY so we can assert we no longer
    # send it from the client; NC ignores the oc:fileid filter-rule.
    body = xml.build_fileid_report(12345).decode()
    assert "oc:filter-files" in body
    assert "<oc:fileid>12345</oc:fileid>" in body


def test_build_systemtag_report_contains_filter() -> None:
    body = xml.build_systemtag_report(7).decode()
    assert "<oc:systemtag>7</oc:systemtag>" in body
    assert "oc:filter-rules" in body


def test_build_systemtags_propfind_props() -> None:
    body = xml.build_systemtags_propfind().decode()
    assert "d:propfind" in body
    assert "oc:id" in body and "oc:display-name" in body


def test_parse_file_report_file() -> None:
    refs = xml.parse_file_report(FILE_REPORT_XML, USER)
    assert len(refs) == 1
    ref = refs[0]
    assert ref.fileid == 12345
    assert ref.path == "Documents/My Archive.zip"  # URL-decoded, prefix stripped
    assert ref.is_dir is False
    assert ref.name == "My Archive.zip"
    assert ref.parent == "Documents"


def test_parse_file_report_folder_is_dir() -> None:
    refs = xml.parse_file_report(FOLDER_REPORT_XML, USER)
    assert refs[0].is_dir is True
    assert refs[0].path == "Photos"


def test_parse_systemtags_skips_root() -> None:
    tags = xml.parse_systemtags(SYSTEMTAGS_XML)
    names = {t.name: t.id for t in tags}
    assert names == {"extract": 7, "zip": 9}


def test_href_to_path_decoding() -> None:
    href = "/remote.php/dav/files/powertools/a%20b/c%2Bd.txt"
    assert xml.href_to_path(href, USER) == "a b/c+d.txt"


def test_parse_content_location_id() -> None:
    assert xml.parse_content_location_id("/remote.php/dav/systemtags/42") == 42
    assert xml.parse_content_location_id("") is None


# --------------------------------------------------------------------------- #
# Client tests (respx)
# --------------------------------------------------------------------------- #


@respx.mock
def test_capabilities_parses_and_caches() -> None:
    route = respx.get(f"{BASE}/ocs/v2.php/cloud/capabilities").mock(
        return_value=httpx.Response(
            200,
            json={"ocs": {"data": {"version": {"major": 33, "minor": 1, "micro": 4}}}},
        )
    )
    with NextcloudClient(_settings()) as c:
        assert c.capabilities() == (33, 1, 4)
        assert c.capabilities() == (33, 1, 4)  # cached
    assert route.call_count == 1
    req = route.calls[0].request
    assert req.headers["OCS-APIRequest"] == "true"
    assert req.headers["Accept"] == "application/json"


@respx.mock
def test_download_uses_basic_auth_and_encoded_segments() -> None:
    url = f"{BASE}/remote.php/dav/files/{USER}/Docs/My%20File.txt"
    route = respx.get(url).mock(return_value=httpx.Response(200, content=b"hello"))
    with NextcloudClient(_settings()) as c:
        assert c.download("Docs/My File.txt") == b"hello"
    auth = route.calls[0].request.headers["Authorization"]
    decoded = base64.b64decode(auth.split(" ", 1)[1]).decode()
    assert decoded == f"{USER}:app-pw"


@respx.mock
def test_upload_puts_bytes() -> None:
    url = f"{BASE}/remote.php/dav/files/{USER}/out/result.zip"
    route = respx.put(url).mock(return_value=httpx.Response(201))
    with NextcloudClient(_settings()) as c:
        c.upload("out/result.zip", b"data")
    assert route.calls[0].request.content == b"data"


@respx.mock
def test_ensure_dir_automkcol_on_nc32_plus() -> None:
    _capabilities_route(respx, major=33)
    route = respx.route(method="MKCOL", url=f"{BASE}/remote.php/dav/files/{USER}/a/b/c").mock(
        return_value=httpx.Response(201)
    )
    with NextcloudClient(_settings()) as c:
        c.ensure_dir("a/b/c")
    assert route.call_count == 1
    assert route.calls[0].request.headers["X-NC-WebDAV-AutoMkcol"] == "1"


@respx.mock
def test_ensure_dir_per_level_on_nc30() -> None:
    _capabilities_route(respx, major=30)
    r1 = respx.route(method="MKCOL", url=f"{BASE}/remote.php/dav/files/{USER}/a").mock(
        return_value=httpx.Response(201)
    )
    r2 = respx.route(method="MKCOL", url=f"{BASE}/remote.php/dav/files/{USER}/a/b").mock(
        return_value=httpx.Response(405)  # exists
    )
    r3 = respx.route(method="MKCOL", url=f"{BASE}/remote.php/dav/files/{USER}/a/b/c").mock(
        return_value=httpx.Response(201)
    )
    with NextcloudClient(_settings()) as c:
        c.ensure_dir("a/b/c")
    assert r1.called and r2.called and r3.called
    assert "X-NC-WebDAV-AutoMkcol" not in r1.calls[0].request.headers


@respx.mock
def test_resolve_fileid_uses_search_and_parses() -> None:
    # The webhook path resolves fileid -> path via the SEARCH method on
    # /remote.php/dav/ (NOT the old filter-files REPORT NC ignores).
    url = f"{BASE}/remote.php/dav/"
    route = respx.route(method="SEARCH", url=url).mock(
        return_value=httpx.Response(207, content=FILE_REPORT_XML.encode())
    )
    with NextcloudClient(_settings()) as c:
        ref = c.resolve_fileid(12345)
    assert ref is not None
    assert ref.fileid == 12345 and ref.path == "Documents/My Archive.zip"
    assert ref.is_dir is False
    body = route.calls[0].request.content.decode()
    assert "d:searchrequest" in body
    assert "<d:literal>12345</d:literal>" in body
    assert f"<d:href>/files/{USER}</d:href>" in body
    assert route.calls[0].request.headers["Content-Type"] == "application/xml"


@respx.mock
def test_resolve_fileid_does_not_send_old_filter_files_report() -> None:
    # Regression: we must NEVER send the old oc:filter-files / <oc:fileid> REPORT,
    # which NC silently ignores (the live bug). Only a SEARCH should fire.
    search = respx.route(method="SEARCH", url=f"{BASE}/remote.php/dav/").mock(
        return_value=httpx.Response(207, content=FILE_REPORT_XML.encode())
    )
    report = respx.route(method="REPORT", url=f"{BASE}/remote.php/dav/files/{USER}/").mock(
        return_value=httpx.Response(207, content=FILE_REPORT_XML.encode())
    )
    with NextcloudClient(_settings()) as c:
        c.resolve_fileid(12345)
    assert search.called
    assert not report.called


@respx.mock
def test_resolve_fileid_folder_sets_is_dir() -> None:
    respx.route(method="SEARCH", url=f"{BASE}/remote.php/dav/").mock(
        return_value=httpx.Response(207, content=FOLDER_REPORT_XML.encode())
    )
    with NextcloudClient(_settings()) as c:
        ref = c.resolve_fileid(777)
    assert ref is not None and ref.is_dir is True and ref.path == "Photos"


@respx.mock
def test_resolve_fileid_not_found_returns_none() -> None:
    empty = '<d:multistatus xmlns:d="DAV:"/>'
    respx.route(method="SEARCH", url=f"{BASE}/remote.php/dav/").mock(
        return_value=httpx.Response(207, content=empty.encode())
    )
    with NextcloudClient(_settings()) as c:
        assert c.resolve_fileid(999) is None


@respx.mock
def test_list_tags_propfind() -> None:
    route = respx.route(method="PROPFIND", url=f"{BASE}/remote.php/dav/systemtags/").mock(
        return_value=httpx.Response(207, content=SYSTEMTAGS_XML.encode())
    )
    with NextcloudClient(_settings()) as c:
        tags = c.list_tags()
    assert {t.name for t in tags} == {"extract", "zip"}
    assert route.calls[0].request.headers["Depth"] == "1"


@respx.mock
def test_tags_on_file_propfind() -> None:
    url = f"{BASE}/remote.php/dav/systemtags-relations/files/12345"
    route = respx.route(method="PROPFIND", url=url).mock(
        return_value=httpx.Response(207, content=SYSTEMTAGS_XML.encode())
    )
    with NextcloudClient(_settings()) as c:
        tags = c.tags_on_file(12345)
    assert {t.name for t in tags} == {"extract", "zip"}
    assert route.called


@respx.mock
def test_ensure_tag_existing() -> None:
    respx.route(method="PROPFIND", url=f"{BASE}/remote.php/dav/systemtags/").mock(
        return_value=httpx.Response(207, content=SYSTEMTAGS_XML.encode())
    )
    with NextcloudClient(_settings()) as c:
        tag = c.ensure_tag("extract")
    assert tag.id == 7 and tag.name == "extract"


@respx.mock
def test_ensure_tag_creates_and_parses_content_location() -> None:
    respx.route(method="PROPFIND", url=f"{BASE}/remote.php/dav/systemtags/").mock(
        return_value=httpx.Response(207, content=SYSTEMTAGS_XML.encode())
    )
    post = respx.post(f"{BASE}/remote.php/dav/systemtags/").mock(
        return_value=httpx.Response(
            201, headers={"Content-Location": "/remote.php/dav/systemtags/55"}
        )
    )
    with NextcloudClient(_settings()) as c:
        tag = c.ensure_tag("render-png")
    assert tag.id == 55 and tag.name == "render-png"
    assert post.called


@respx.mock
def test_ensure_tag_409_treated_as_exists() -> None:
    # First PROPFIND lacks the tag; POST returns 409; re-list now includes it.
    without = SYSTEMTAGS_XML
    with_new = SYSTEMTAGS_XML.replace(
        "</d:multistatus>",
        '<d:response><d:href>/remote.php/dav/systemtags/99</d:href>'
        "<d:propstat><d:prop><oc:id>99</oc:id>"
        "<oc:display-name>render</oc:display-name></d:prop>"
        "<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response></d:multistatus>",
    )
    respx.route(method="PROPFIND", url=f"{BASE}/remote.php/dav/systemtags/").mock(
        side_effect=[
            httpx.Response(207, content=without.encode()),
            httpx.Response(207, content=with_new.encode()),
        ]
    )
    respx.post(f"{BASE}/remote.php/dav/systemtags/").mock(return_value=httpx.Response(409))
    with NextcloudClient(_settings()) as c:
        tag = c.ensure_tag("render")
    assert tag.id == 99 and tag.name == "render"


@respx.mock
def test_search_by_tag_report() -> None:
    url = f"{BASE}/remote.php/dav/files/{USER}/"
    route = respx.route(method="REPORT", url=url).mock(
        return_value=httpx.Response(207, content=FILE_REPORT_XML.encode())
    )
    with NextcloudClient(_settings()) as c:
        refs = c.search_by_tag(7)
    assert refs[0].fileid == 12345
    assert "<oc:systemtag>7</oc:systemtag>" in route.calls[0].request.content.decode()


@respx.mock
def test_assign_tag_put() -> None:
    url = f"{BASE}/remote.php/dav/systemtags-relations/files/12345/7"
    route = respx.put(url).mock(return_value=httpx.Response(201))
    with NextcloudClient(_settings()) as c:
        c.assign_tag(12345, 7)
    assert route.called


@respx.mock
def test_remove_tag_delete() -> None:
    url = f"{BASE}/remote.php/dav/systemtags-relations/files/12345/7"
    route = respx.delete(url).mock(return_value=httpx.Response(204))
    with NextcloudClient(_settings()) as c:
        c.remove_tag(12345, 7)
    assert route.called


@respx.mock
def test_notify_disabled_by_default_does_nothing() -> None:
    route = respx.post(
        f"{BASE}/ocs/v2.php/apps/notifications/api/v2/admin_notifications/alice"
    ).mock(return_value=httpx.Response(200, json={}))
    with NextcloudClient(_settings()) as c:  # NOTIFY defaults False
        c.notify("alice", "hi")
    assert not route.called


@respx.mock
def test_notify_sends_when_enabled() -> None:
    s = Settings(NEXTCLOUD_URL=BASE, NC_USER=USER, NC_APP_PASSWORD="app-pw", NOTIFY=True)
    route = respx.post(
        f"{BASE}/ocs/v2.php/apps/notifications/api/v2/admin_notifications/alice"
    ).mock(return_value=httpx.Response(200, json={}))
    with NextcloudClient(s) as c:
        c.notify("alice", "done", "long body")
    assert route.called
    req = route.calls[0].request
    assert req.headers["OCS-APIRequest"] == "true"
    assert b"shortMessage" in req.content


@respx.mock
def test_notify_swallows_failure() -> None:
    s = Settings(NEXTCLOUD_URL=BASE, NC_USER=USER, NC_APP_PASSWORD="app-pw", NOTIFY=True)
    respx.post(
        f"{BASE}/ocs/v2.php/apps/notifications/api/v2/admin_notifications/alice"
    ).mock(return_value=httpx.Response(500))
    with NextcloudClient(s) as c:
        c.notify("alice", "done")  # must not raise


@respx.mock
def test_non_2xx_raises_ncapierror_with_status_and_url() -> None:
    url = f"{BASE}/remote.php/dav/files/{USER}/missing.txt"
    respx.get(url).mock(return_value=httpx.Response(404, text="Not Found"))
    with NextcloudClient(_settings()) as c, pytest.raises(NcApiError) as exc:
        c.download("missing.txt")
    assert exc.value.status == 404
    assert exc.value.url is not None and "missing.txt" in exc.value.url


# --------------------------------------------------------------------------- #
# shred / trash XML parsers + capability flags (F5)
# --------------------------------------------------------------------------- #


def test_parse_shred_props_local_file() -> None:
    body = (
        '<?xml version="1.0"?>'
        '<d:multistatus xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns" '
        'xmlns:nc="http://nextcloud.org/ns">'
        f"<d:response><d:href>/remote.php/dav/files/{USER}/Shredder/a</d:href>"
        "<d:propstat><d:prop><oc:fileid>55</oc:fileid><oc:size>4096</oc:size>"
        "<oc:share-types/><nc:mount-type/><d:resourcetype/></d:prop>"
        "<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
        "</d:multistatus>"
    )
    props = xml.parse_shred_props(body)
    assert props["fileid"] == 55
    assert props["size"] == 4096
    assert props["share_types"] == []
    assert props["mount_type"] is None
    assert props["is_dir"] is False


def test_parse_shred_props_detects_share_and_mount() -> None:
    body = (
        '<?xml version="1.0"?>'
        '<d:multistatus xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns" '
        'xmlns:nc="http://nextcloud.org/ns">'
        f"<d:response><d:href>/remote.php/dav/files/{USER}/Shredder/b</d:href>"
        "<d:propstat><d:prop><oc:fileid>56</oc:fileid>"
        "<oc:share-types><oc:share-type>0</oc:share-type></oc:share-types>"
        "<nc:mount-type>shared</nc:mount-type>"
        "<d:resourcetype><d:collection/></d:resourcetype></d:prop>"
        "<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
        "</d:multistatus>"
    )
    props = xml.parse_shred_props(body)
    assert props["share_types"] == [0]
    assert props["mount_type"] == "shared"
    assert props["is_dir"] is True


def test_parse_trash_items_takes_node_from_href() -> None:
    body = (
        '<?xml version="1.0"?>'
        '<d:multistatus xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns" '
        'xmlns:nc="http://nextcloud.org/ns">'
        f"<d:response><d:href>/remote.php/dav/trashbin/{USER}/trash/</d:href>"
        "<d:propstat><d:prop/><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
        f"<d:response><d:href>/remote.php/dav/trashbin/{USER}/trash/doomed.d999</d:href>"
        "<d:propstat><d:prop><oc:fileid>91</oc:fileid>"
        "<nc:trashbin-original-location>Shredder/doomed</nc:trashbin-original-location>"
        "<nc:trashbin-deletion-time>999</nc:trashbin-deletion-time></d:prop>"
        "<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
        "</d:multistatus>"
    )
    items = xml.parse_trash_items(body)
    # the collection root response is skipped; one real item remains
    assert len(items) == 1
    assert items[0]["node_name"] == "doomed.d999"
    assert items[0]["fileid"] == 91
    assert items[0]["original_location"] == "Shredder/doomed"
    assert items[0]["deletion_time"] == 999


@respx.mock
def test_files_capabilities_defaults_missing_to_true() -> None:
    respx.get(f"{BASE}/ocs/v2.php/cloud/capabilities").mock(
        return_value=httpx.Response(
            200,
            json={
                "ocs": {
                    "data": {
                        "version": {"major": 33, "minor": 0, "micro": 0},
                        "capabilities": {"files": {"undelete": True, "delete_from_trash": False}},
                    }
                }
            },
        )
    )
    with NextcloudClient(_settings()) as c:
        caps = c.files_capabilities()
    assert caps["undelete"] is True
    assert caps["delete_from_trash"] is False
    # missing keys default to True (older-server tolerant)
    assert caps["versioning"] is True
    assert caps["version_deletion"] is True

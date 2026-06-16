"""Tests for the DESTRUCTIVE shred feature (F5) — all respx-mocked, never live.

Covers:
* disabled by default (a ``shred`` tag issues NO DELETE);
* step-1 writes the CONFIRM receipt + removes the shred tag, NO file DELETE;
* scope guards (outside SHRED_DIR, the dir root, account root, ``..``, shares/mounts)
  refuse with no receipt and no delete;
* step-2 happy path: exact DELETE files -> PROPFIND trash -> DELETE trash-node
  sequence (node taken from the fileid match), SHREDDED receipt, confirm-tag removed,
  and NO overwrite PUT ever issued;
* step-2 guards: fileid mismatch aborts, delete_from_trash=false -> FAILED, trash
  item not found -> failure (no silent success);
* shred-confirm on a non-receipt file is ignored.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import respx

from ncpowertools import locking
from ncpowertools.config import Settings
from ncpowertools.models import FileRef, TagEvent
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
        "ERROR_TAG": "",
        "NOTIFY": False,
        "POLL_INTERVAL": 0,
        "ENABLE_SHRED": True,
        "SHRED_DIR": "Shredder",
    }
    kw.update(over)
    return Settings(**kw)  # type: ignore[arg-type]


def _caps(mock: respx.MockRouter, *, undelete: bool = True, delete_from_trash: bool = True) -> None:
    mock.get(f"{BASE}/ocs/v2.php/cloud/capabilities").mock(
        return_value=httpx.Response(
            200,
            json={
                "ocs": {
                    "data": {
                        "version": {"major": 33, "minor": 0, "micro": 0},
                        "capabilities": {
                            "files": {
                                "undelete": undelete,
                                "delete_from_trash": delete_from_trash,
                                "versioning": True,
                                "version_deletion": True,
                            }
                        },
                    }
                }
            },
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


def _shred_props(
    fileid: int,
    *,
    is_dir: bool = False,
    size: int = 1234,
    mount: str | None = None,
    share_types: list[int] | None = None,
) -> bytes:
    rt = "<d:resourcetype><d:collection/></d:resourcetype>" if is_dir else "<d:resourcetype/>"
    mount_el = f"<nc:mount-type>{mount}</nc:mount-type>" if mount else "<nc:mount-type/>"
    if share_types:
        inner = "".join(f"<oc:share-type>{s}</oc:share-type>" for s in share_types)
        share_el = f"<oc:share-types>{inner}</oc:share-types>"
    else:
        share_el = "<oc:share-types/>"
    return (
        '<?xml version="1.0"?>'
        '<d:multistatus xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns" '
        'xmlns:nc="http://nextcloud.org/ns">'
        f"<d:response><d:href>/remote.php/dav/files/{USER}/Shredder/x</d:href>"
        f"<d:propstat><d:prop><oc:fileid>{fileid}</oc:fileid>"
        f"<oc:size>{size}</oc:size>{share_el}{mount_el}{rt}</d:prop>"
        "<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
        "</d:multistatus>"
    ).encode()


def _trash_list(*items: tuple[str, int, str, int]) -> bytes:
    """items = (node_name, fileid, original_location, deletion_time)."""
    rows = "".join(
        f"<d:response><d:href>/remote.php/dav/trashbin/{USER}/trash/{node}</d:href>"
        f"<d:propstat><d:prop><oc:fileid>{fid}</oc:fileid>"
        f"<nc:trashbin-original-location>{orig}</nc:trashbin-original-location>"
        f"<nc:trashbin-deletion-time>{dt}</nc:trashbin-deletion-time>"
        "</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
        for node, fid, orig, dt in items
    )
    root = (
        f"<d:response><d:href>/remote.php/dav/trashbin/{USER}/trash/</d:href>"
        "<d:propstat><d:prop/><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
    )
    return (
        '<?xml version="1.0"?>'
        '<d:multistatus xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns" '
        'xmlns:nc="http://nextcloud.org/ns">'
        f"{root}{rows}</d:multistatus>"
    ).encode()


def _receipt_md(target_path: str, target_fileid: int, *, is_dir: bool = False) -> bytes:
    return (
        "# CONFIRM SHRED\n\n"
        "```ncpowertools-shred\n"
        f"target_path: {target_path}\n"
        f"target_fileid: {target_fileid}\n"
        f"target_is_dir: {is_dir}\n"
        "```\n"
    ).encode()


def _no_file_delete(calls: respx.MockRouter) -> None:
    """Assert no DELETE was issued against the files namespace (user content)."""
    for call in calls:
        if call.request.method == "DELETE":
            p = call.request.url.path
            assert "/remote.php/dav/files/" not in p, f"unexpected file DELETE: {p}"


def _no_overwrite_put(calls: respx.MockRouter, target_path: str) -> None:
    for call in calls:
        if call.request.method == "PUT":
            assert not call.request.url.path.endswith(target_path), (
                f"unexpected overwrite PUT on target: {target_path}"
            )


# --------------------------------------------------------------------------- #
# disabled by default
# --------------------------------------------------------------------------- #


@respx.mock
def test_shred_disabled_by_default_ignores_tag(tmp_path: Path) -> None:
    locking._reset()
    # ENABLE_SHRED defaults false; but the tag must still be a configured action
    # for the pipeline to even consider it. With shred disabled, the shred tags
    # are NOT in TAG_ACTIONS, so _match_action returns None -> skip. We also force
    # the action to verify the pipeline guard rejects it even if reached.
    settings = Settings(
        NEXTCLOUD_URL=BASE,
        NC_USER=USER,
        NC_APP_PASSWORD="app-pw",
        WORK_DIR=str(tmp_path / "work"),
        POLL_INTERVAL=0,
        ENABLE_SHRED=False,
        TAG_ACTIONS={"shred": "shred"},  # force the mapping to reach the guard
    )
    _caps(respx)
    respx.route(
        method="PROPFIND", url=f"{BASE}/remote.php/dav/systemtags-relations/files/50"
    ).mock(return_value=httpx.Response(207, content=_tags_relation((1, "shred"))))

    src = FileRef(fileid=50, path="Shredder/secret.txt")
    with NextcloudClient(settings) as client:
        Pipeline(client, settings).process(
            TagEvent(uid=USER, fileids=[50], tagids=[1], files=[src])
        )

    # No DELETE, no PUT — the tag is ignored.
    assert not any(c.request.method == "DELETE" for c in respx.calls)
    assert not any(c.request.method == "PUT" for c in respx.calls)


# --------------------------------------------------------------------------- #
# step 1: write CONFIRM receipt, remove shred tag, NO file delete
# --------------------------------------------------------------------------- #


@respx.mock
def test_step1_writes_receipt_and_removes_tag_no_delete(tmp_path: Path) -> None:
    locking._reset()
    settings = _settings(tmp_path)
    _caps(respx)
    respx.route(
        method="PROPFIND", url=f"{BASE}/remote.php/dav/systemtags-relations/files/60"
    ).mock(return_value=httpx.Response(207, content=_tags_relation((1, "shred"))))
    # guard PROPFIND on the target (Depth 0)
    respx.route(
        method="PROPFIND", url=f"{BASE}/remote.php/dav/files/{USER}/Shredder/secret.txt"
    ).mock(return_value=httpx.Response(207, content=_shred_props(60)))
    respx.route(method="MKCOL").mock(return_value=httpx.Response(405))
    receipt_put = respx.put(
        url__regex=rf"{BASE}/remote.php/dav/files/{USER}/Shredder/CONFIRM-SHRED-60-.*\.md"
    ).mock(return_value=httpx.Response(201))
    untag = respx.delete(
        f"{BASE}/remote.php/dav/systemtags-relations/files/60/1"
    ).mock(return_value=httpx.Response(204))

    src = FileRef(fileid=60, path="Shredder/secret.txt")
    with NextcloudClient(settings) as client:
        Pipeline(client, settings).process(
            TagEvent(uid=USER, fileids=[60], tagids=[1], files=[src])
        )

    assert receipt_put.called
    body = receipt_put.calls[0].request.content.decode()
    assert "target_path: Shredder/secret.txt" in body
    assert "target_fileid: 60" in body
    assert "PERMANENTLY" in body
    assert untag.call_count == 1  # shred tag removed from target
    _no_file_delete(respx.calls)  # NO file DELETE in step 1


# --------------------------------------------------------------------------- #
# scope guards (step 1) — refuse, no receipt, no delete
# --------------------------------------------------------------------------- #


def _run_step1(tmp_path: Path, target_path: str, props: bytes | None = None,
               fileid: int = 70) -> respx.MockRouter:
    """Drive step 1 for a target and return the respx router for assertions."""
    settings = _settings(tmp_path)
    _caps(respx)
    respx.route(
        method="PROPFIND",
        url=f"{BASE}/remote.php/dav/systemtags-relations/files/{fileid}",
    ).mock(return_value=httpx.Response(207, content=_tags_relation((1, "shred"))))
    if props is not None:
        respx.route(
            method="PROPFIND",
            url=f"{BASE}/remote.php/dav/files/{USER}/{target_path}",
        ).mock(return_value=httpx.Response(207, content=props))
    src = FileRef(fileid=fileid, path=target_path)
    with NextcloudClient(settings) as client:
        Pipeline(client, settings).process(
            TagEvent(uid=USER, fileids=[fileid], tagids=[1], files=[src])
        )
    return respx


@respx.mock
def test_guard_outside_shred_dir_refused(tmp_path: Path) -> None:
    locking._reset()
    _run_step1(tmp_path, "Documents/secret.txt")  # no props -> guard fails first
    assert not any(c.request.method == "PUT" for c in respx.calls)
    assert not any(c.request.method == "DELETE" for c in respx.calls)


@respx.mock
def test_guard_shred_dir_root_refused(tmp_path: Path) -> None:
    locking._reset()
    _run_step1(tmp_path, "Shredder")
    assert not any(c.request.method == "PUT" for c in respx.calls)
    assert not any(c.request.method == "DELETE" for c in respx.calls)


@respx.mock
def test_guard_account_root_refused(tmp_path: Path) -> None:
    locking._reset()
    _run_step1(tmp_path, "")
    assert not any(c.request.method == "PUT" for c in respx.calls)
    assert not any(c.request.method == "DELETE" for c in respx.calls)


@respx.mock
def test_guard_dotdot_refused(tmp_path: Path) -> None:
    locking._reset()
    _run_step1(tmp_path, "Shredder/../etc/passwd")
    assert not any(c.request.method == "PUT" for c in respx.calls)
    assert not any(c.request.method == "DELETE" for c in respx.calls)


@respx.mock
def test_guard_shared_mount_refused(tmp_path: Path) -> None:
    locking._reset()
    _run_step1(
        tmp_path,
        "Shredder/shared.txt",
        props=_shred_props(70, mount="external"),
    )
    # PROPFIND happened, but the share/mount guard refused -> no receipt, no delete.
    assert not any(c.request.method == "PUT" for c in respx.calls)
    assert not any(c.request.method == "DELETE" for c in respx.calls)


@respx.mock
def test_guard_share_types_refused(tmp_path: Path) -> None:
    locking._reset()
    _run_step1(
        tmp_path,
        "Shredder/recv.txt",
        props=_shred_props(70, share_types=[0]),
    )
    assert not any(c.request.method == "PUT" for c in respx.calls)
    assert not any(c.request.method == "DELETE" for c in respx.calls)


# --------------------------------------------------------------------------- #
# step 2: happy path — exact DELETE / PROPFIND trash / DELETE node sequence
# --------------------------------------------------------------------------- #


@respx.mock
def test_step2_happy_path_purges_in_sequence(tmp_path: Path) -> None:
    locking._reset()
    settings = _settings(tmp_path)
    _caps(respx)
    target = "Shredder/doomed.bin"
    target_fileid = 91
    receipt_path = "Shredder/CONFIRM-SHRED-91-doomed.bin.md"

    # the confirm tag is on the receipt file (fileid 200)
    respx.route(
        method="PROPFIND", url=f"{BASE}/remote.php/dav/systemtags-relations/files/200"
    ).mock(return_value=httpx.Response(207, content=_tags_relation((2, "shred-confirm"))))
    # read the receipt body
    respx.get(f"{BASE}/remote.php/dav/files/{USER}/{receipt_path}").mock(
        return_value=httpx.Response(200, content=_receipt_md(target, target_fileid))
    )
    # re-resolve guard PROPFIND on the target -> fileid matches
    respx.route(
        method="PROPFIND", url=f"{BASE}/remote.php/dav/files/{USER}/{target}"
    ).mock(return_value=httpx.Response(207, content=_shred_props(target_fileid)))
    # DELETE target -> trash
    del_target = respx.delete(f"{BASE}/remote.php/dav/files/{USER}/{target}").mock(
        return_value=httpx.Response(204)
    )
    # PROPFIND trash -> the node carrying the matching fileid
    trash = respx.route(
        method="PROPFIND", url=f"{BASE}/remote.php/dav/trashbin/{USER}/trash"
    ).mock(
        return_value=httpx.Response(
            207,
            content=_trash_list(
                ("other.d111", 5, "Other/o", 111),
                ("doomed.bin.d999", target_fileid, target, 999),
            ),
        )
    )
    # permanent purge of that trash node
    del_node = respx.delete(
        f"{BASE}/remote.php/dav/trashbin/{USER}/trash/doomed.bin.d999"
    ).mock(return_value=httpx.Response(204))
    respx.route(method="MKCOL").mock(return_value=httpx.Response(405))
    shredded_put = respx.put(
        url__regex=rf"{BASE}/remote.php/dav/files/{USER}/Shredder/SHREDDED-.*\.md"
    ).mock(return_value=httpx.Response(201))
    # the obsolete CONFIRM receipt is deleted afterwards
    del_receipt = respx.delete(
        f"{BASE}/remote.php/dav/files/{USER}/{receipt_path}"
    ).mock(return_value=httpx.Response(204))
    untag = respx.delete(
        f"{BASE}/remote.php/dav/systemtags-relations/files/200/2"
    ).mock(return_value=httpx.Response(204))

    receipt = FileRef(fileid=200, path=receipt_path)
    with NextcloudClient(settings) as client:
        Pipeline(client, settings).process(
            TagEvent(uid=USER, fileids=[200], tagids=[2], files=[receipt])
        )

    assert del_target.called
    assert trash.called
    assert del_node.called  # the matched-by-fileid node was purged
    assert shredded_put.called
    assert del_receipt.called
    assert untag.call_count == 1
    # The node purged was selected by the fileid match, not constructed.
    assert del_node.calls[0].request.url.path.endswith("/trash/doomed.bin.d999")
    # NO overwrite PUT on the target was ever issued.
    _no_overwrite_put(respx.calls, target)
    assert not any(
        c.request.method == "PUT" and c.request.url.path.endswith(target)
        for c in respx.calls
    )


# --------------------------------------------------------------------------- #
# step 2 guards
# --------------------------------------------------------------------------- #


@respx.mock
def test_step2_fileid_mismatch_aborts_no_delete(tmp_path: Path) -> None:
    locking._reset()
    settings = _settings(tmp_path)
    _caps(respx)
    target = "Shredder/doomed.bin"
    receipt_path = "Shredder/CONFIRM-SHRED-91-doomed.bin.md"
    respx.route(
        method="PROPFIND", url=f"{BASE}/remote.php/dav/systemtags-relations/files/200"
    ).mock(return_value=httpx.Response(207, content=_tags_relation((2, "shred-confirm"))))
    respx.get(f"{BASE}/remote.php/dav/files/{USER}/{receipt_path}").mock(
        return_value=httpx.Response(200, content=_receipt_md(target, 91))
    )
    # resolved fileid (42) != receipt fileid (91) -> abort
    respx.route(
        method="PROPFIND", url=f"{BASE}/remote.php/dav/files/{USER}/{target}"
    ).mock(return_value=httpx.Response(207, content=_shred_props(42)))
    del_target = respx.delete(f"{BASE}/remote.php/dav/files/{USER}/{target}")
    respx.route(method="MKCOL").mock(return_value=httpx.Response(405))
    failed_put = respx.put(
        url__regex=rf"{BASE}/remote.php/dav/files/{USER}/Shredder/FAILED-SHRED-.*\.md"
    ).mock(return_value=httpx.Response(201))
    respx.delete(f"{BASE}/remote.php/dav/systemtags-relations/files/200/2").mock(
        return_value=httpx.Response(204)
    )

    receipt = FileRef(fileid=200, path=receipt_path)
    with NextcloudClient(settings) as client:
        Pipeline(client, settings).process(
            TagEvent(uid=USER, fileids=[200], tagids=[2], files=[receipt])
        )

    assert not del_target.called  # no destructive delete
    assert failed_put.called  # FAILED note written


@respx.mock
def test_step2_delete_from_trash_disabled_fails_no_delete(tmp_path: Path) -> None:
    locking._reset()
    settings = _settings(tmp_path)
    _caps(respx, undelete=True, delete_from_trash=False)
    target = "Shredder/doomed.bin"
    receipt_path = "Shredder/CONFIRM-SHRED-91-doomed.bin.md"
    respx.route(
        method="PROPFIND", url=f"{BASE}/remote.php/dav/systemtags-relations/files/200"
    ).mock(return_value=httpx.Response(207, content=_tags_relation((2, "shred-confirm"))))
    respx.get(f"{BASE}/remote.php/dav/files/{USER}/{receipt_path}").mock(
        return_value=httpx.Response(200, content=_receipt_md(target, 91))
    )
    respx.route(
        method="PROPFIND", url=f"{BASE}/remote.php/dav/files/{USER}/{target}"
    ).mock(return_value=httpx.Response(207, content=_shred_props(91)))
    del_target = respx.delete(f"{BASE}/remote.php/dav/files/{USER}/{target}")
    respx.route(method="MKCOL").mock(return_value=httpx.Response(405))
    failed_put = respx.put(
        url__regex=rf"{BASE}/remote.php/dav/files/{USER}/Shredder/FAILED-SHRED-.*\.md"
    ).mock(return_value=httpx.Response(201))
    respx.delete(f"{BASE}/remote.php/dav/systemtags-relations/files/200/2").mock(
        return_value=httpx.Response(204)
    )

    receipt = FileRef(fileid=200, path=receipt_path)
    with NextcloudClient(settings) as client:
        Pipeline(client, settings).process(
            TagEvent(uid=USER, fileids=[200], tagids=[2], files=[receipt])
        )

    assert not del_target.called  # cannot purge -> never touched the file
    assert failed_put.called


@respx.mock
def test_step2_trash_item_not_found_reports_failure(tmp_path: Path) -> None:
    locking._reset()
    settings = _settings(tmp_path)
    _caps(respx)
    target = "Shredder/doomed.bin"
    receipt_path = "Shredder/CONFIRM-SHRED-91-doomed.bin.md"
    respx.route(
        method="PROPFIND", url=f"{BASE}/remote.php/dav/systemtags-relations/files/200"
    ).mock(return_value=httpx.Response(207, content=_tags_relation((2, "shred-confirm"))))
    respx.get(f"{BASE}/remote.php/dav/files/{USER}/{receipt_path}").mock(
        return_value=httpx.Response(200, content=_receipt_md(target, 91))
    )
    respx.route(
        method="PROPFIND", url=f"{BASE}/remote.php/dav/files/{USER}/{target}"
    ).mock(return_value=httpx.Response(207, content=_shred_props(91)))
    del_target = respx.delete(f"{BASE}/remote.php/dav/files/{USER}/{target}").mock(
        return_value=httpx.Response(204)
    )
    # trash list has NO matching fileid and NO matching original-location
    respx.route(
        method="PROPFIND", url=f"{BASE}/remote.php/dav/trashbin/{USER}/trash"
    ).mock(return_value=httpx.Response(207, content=_trash_list(("other.d1", 7, "X/y", 1))))
    del_node = respx.route(
        method="DELETE", url__regex=rf"{BASE}/remote.php/dav/trashbin/{USER}/trash/.+"
    )
    respx.route(method="MKCOL").mock(return_value=httpx.Response(405))
    failed_put = respx.put(
        url__regex=rf"{BASE}/remote.php/dav/files/{USER}/Shredder/FAILED-SHRED-.*\.md"
    ).mock(return_value=httpx.Response(201))
    respx.delete(f"{BASE}/remote.php/dav/systemtags-relations/files/200/2").mock(
        return_value=httpx.Response(204)
    )

    receipt = FileRef(fileid=200, path=receipt_path)
    with NextcloudClient(settings) as client:
        Pipeline(client, settings).process(
            TagEvent(uid=USER, fileids=[200], tagids=[2], files=[receipt])
        )

    assert del_target.called  # the file IS in trash now
    assert not del_node.called  # but no node was purged (not found)
    assert failed_put.called  # reported as a failure, not silent success


# --------------------------------------------------------------------------- #
# shred-confirm on a non-receipt file -> ignored
# --------------------------------------------------------------------------- #


@respx.mock
def test_confirm_on_non_receipt_ignored(tmp_path: Path) -> None:
    locking._reset()
    settings = _settings(tmp_path)
    _caps(respx)
    # confirm tag on a normal file (not a CONFIRM-SHRED receipt)
    respx.route(
        method="PROPFIND", url=f"{BASE}/remote.php/dav/systemtags-relations/files/201"
    ).mock(return_value=httpx.Response(207, content=_tags_relation((2, "shred-confirm"))))
    untag = respx.delete(
        f"{BASE}/remote.php/dav/systemtags-relations/files/201/2"
    ).mock(return_value=httpx.Response(204))

    receipt = FileRef(fileid=201, path="Shredder/just-a-note.md")
    with NextcloudClient(settings) as client:
        Pipeline(client, settings).process(
            TagEvent(uid=USER, fileids=[201], tagids=[2], files=[receipt])
        )

    # No file delete, no GET of the file body, confirm tag removed.
    _no_file_delete(respx.calls)
    assert not any(c.request.method == "GET" and "just-a-note" in c.request.url.path
                   for c in respx.calls)
    assert untag.call_count == 1

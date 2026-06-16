"""Pure functions to BUILD and PARSE the WebDAV REPORT/PROPFIND XML.

No I/O here — these are deterministic transforms used by ``client.py`` and
exercised directly in unit tests. lxml is used for namespace ergonomics.

Three namespaces are in play everywhere (CONTEXT.md §2):
  DAV:                          -> d
  http://owncloud.org/ns        -> oc
  http://nextcloud.org/ns       -> nc
"""

from __future__ import annotations

from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import unquote

from lxml import etree

from ..models import FileRef, TagSpec

NS_D = "DAV:"
NS_OC = "http://owncloud.org/ns"
NS_NC = "http://nextcloud.org/ns"

NSMAP = {"d": NS_D, "oc": NS_OC, "nc": NS_NC}

_XML_DECL = b'<?xml version="1.0" encoding="UTF-8"?>\n'


def _qn(prefix: str, local: str) -> str:
    return f"{{{NSMAP[prefix]}}}{local}"


def _serialize(root: etree._Element) -> bytes:
    return _XML_DECL + etree.tostring(root)


# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #


def _filter_files_root() -> etree._Element:
    return etree.Element(_qn("oc", "filter-files"), nsmap=NSMAP)


def _standard_props(parent: etree._Element) -> None:
    """Append the prop set we request on file REPORTs."""
    prop = etree.SubElement(parent, _qn("d", "prop"))
    etree.SubElement(prop, _qn("oc", "fileid"))
    etree.SubElement(prop, _qn("d", "getcontenttype"))
    etree.SubElement(prop, _qn("d", "getlastmodified"))
    etree.SubElement(prop, _qn("d", "resourcetype"))


def build_fileid_report(fileid: int) -> bytes:
    """DEPRECATED / DO NOT USE — kept only as a regression guard.

    This builds an ``oc:filter-files`` REPORT with an ``<oc:fileid>`` filter-rule.
    **Nextcloud silently ignores the ``oc:fileid`` filter-rule** (verified live on
    NC 33.0.5: the REPORT returns an empty multistatus), so resolving a fileid this
    way always yields "not found" and nothing is processed. This was a research
    error in the original CONTEXT.md §2. The supported fileid -> path resolver on
    Nextcloud is the WebDAV **SEARCH** method (:func:`build_fileid_search`); the
    ``oc:fileid`` filter is only honoured inside an ``<oc:systemtag>`` search
    (:func:`build_systemtag_report`), which is why the poller path works.

    Do not re-introduce this into the client. It exists solely so a test can assert
    we no longer send it.
    """
    root = _filter_files_root()
    _standard_props(root)
    rules = etree.SubElement(root, _qn("oc", "filter-rules"))
    etree.SubElement(rules, _qn("oc", "fileid")).text = str(fileid)
    return _serialize(root)


def build_fileid_search(fileid: int, user: str) -> bytes:
    """SEARCH body resolving a ``fileid`` -> path (the supported NC resolver).

    Nextcloud does NOT expose ownCloud's ``/remote.php/dav/meta/{fileid}`` endpoint
    (no ``Meta`` collection exists in NC's DAV ``RootCollection``), and the
    ``oc:fileid`` *filter-rule* on ``oc:filter-files`` is ignored. The documented,
    supported way to map a fileid to its path on Nextcloud is the WebDAV ``SEARCH``
    method against ``/remote.php/dav/`` with a ``<d:basicsearch>`` whose ``<d:where>``
    matches ``<oc:fileid>`` (``oc:fileid`` is both *selectable* and *searchable*).

    We scope the search to ``/files/<user>`` (the user's own namespace) at
    ``depth: infinity`` and select the props needed to build a :class:`FileRef` in
    one round-trip — including ``<d:resourcetype/>`` so ``is_dir`` is known without a
    follow-up PROPFIND.

    See: NC Developer Manual → WebDAV → Search.
    """
    root = etree.Element(_qn("d", "searchrequest"), nsmap=NSMAP)
    basic = etree.SubElement(root, _qn("d", "basicsearch"))

    select = etree.SubElement(basic, _qn("d", "select"))
    prop = etree.SubElement(select, _qn("d", "prop"))
    etree.SubElement(prop, _qn("oc", "fileid"))
    etree.SubElement(prop, _qn("d", "getcontenttype"))
    etree.SubElement(prop, _qn("d", "getlastmodified"))
    etree.SubElement(prop, _qn("d", "resourcetype"))

    from_el = etree.SubElement(basic, _qn("d", "from"))
    scope = etree.SubElement(from_el, _qn("d", "scope"))
    etree.SubElement(scope, _qn("d", "href")).text = f"/files/{user}"
    etree.SubElement(scope, _qn("d", "depth")).text = "infinity"

    where = etree.SubElement(basic, _qn("d", "where"))
    eq = etree.SubElement(where, _qn("d", "eq"))
    eq_prop = etree.SubElement(eq, _qn("d", "prop"))
    etree.SubElement(eq_prop, _qn("oc", "fileid"))
    etree.SubElement(eq, _qn("d", "literal")).text = str(fileid)

    etree.SubElement(basic, _qn("d", "orderby"))
    return _serialize(root)


def build_systemtag_report(tagid: int) -> bytes:
    """REPORT body filtering by ``oc:systemtag`` (all files carrying a tag)."""
    root = _filter_files_root()
    _standard_props(root)
    rules = etree.SubElement(root, _qn("oc", "filter-rules"))
    etree.SubElement(rules, _qn("oc", "systemtag")).text = str(tagid)
    return _serialize(root)


def build_systemtags_propfind() -> bytes:
    """PROPFIND body to list all system tags + ids."""
    root = etree.Element(_qn("d", "propfind"), nsmap=NSMAP)
    prop = etree.SubElement(root, _qn("d", "prop"))
    etree.SubElement(prop, _qn("oc", "id"))
    etree.SubElement(prop, _qn("oc", "display-name"))
    etree.SubElement(prop, _qn("oc", "user-visible"))
    etree.SubElement(prop, _qn("oc", "user-assignable"))
    etree.SubElement(prop, _qn("oc", "can-assign"))
    return _serialize(root)


def build_relations_propfind() -> bytes:
    """PROPFIND body for the tags assigned to a single file (relations)."""
    root = etree.Element(_qn("d", "propfind"), nsmap=NSMAP)
    prop = etree.SubElement(root, _qn("d", "prop"))
    etree.SubElement(prop, _qn("oc", "id"))
    etree.SubElement(prop, _qn("oc", "display-name"))
    etree.SubElement(prop, _qn("oc", "user-visible"))
    etree.SubElement(prop, _qn("oc", "user-assignable"))
    etree.SubElement(prop, _qn("oc", "can-assign"))
    return _serialize(root)


def build_shred_propfind() -> bytes:
    """PROPFIND body for a shred target's guard props (size/share/mount + id).

    Requests the props the shred scope guards need to inspect on the target
    (F5): ``oc:fileid`` (verify identity), ``oc:size`` (recursive size for the
    receipt), ``oc:share-types`` (refuse received shares) and ``nc:mount-type``
    (refuse external/group/non-local mounts), plus ``d:resourcetype`` for
    is_dir. ``Depth: 0`` — the target itself only.
    """
    root = etree.Element(_qn("d", "propfind"), nsmap=NSMAP)
    prop = etree.SubElement(root, _qn("d", "prop"))
    etree.SubElement(prop, _qn("oc", "fileid"))
    etree.SubElement(prop, _qn("oc", "size"))
    etree.SubElement(prop, _qn("oc", "share-types"))
    etree.SubElement(prop, _qn("nc", "mount-type"))
    etree.SubElement(prop, _qn("d", "resourcetype"))
    return _serialize(root)


def build_count_propfind() -> bytes:
    """PROPFIND body to count files under a folder (``oc:fileid`` per member)."""
    root = etree.Element(_qn("d", "propfind"), nsmap=NSMAP)
    prop = etree.SubElement(root, _qn("d", "prop"))
    etree.SubElement(prop, _qn("oc", "fileid"))
    etree.SubElement(prop, _qn("d", "resourcetype"))
    return _serialize(root)


def build_lastmodified_propfind() -> bytes:
    """PROPFIND body requesting just ``d:getlastmodified`` (Immich mtime, F6)."""
    root = etree.Element(_qn("d", "propfind"), nsmap=NSMAP)
    prop = etree.SubElement(root, _qn("d", "prop"))
    etree.SubElement(prop, _qn("d", "getlastmodified"))
    return _serialize(root)


def build_trash_propfind() -> bytes:
    """PROPFIND body listing a user's trash items with the props shred needs."""
    root = etree.Element(_qn("d", "propfind"), nsmap=NSMAP)
    prop = etree.SubElement(root, _qn("d", "prop"))
    etree.SubElement(prop, _qn("oc", "fileid"))
    etree.SubElement(prop, _qn("nc", "trashbin-original-location"))
    etree.SubElement(prop, _qn("nc", "trashbin-filename"))
    etree.SubElement(prop, _qn("nc", "trashbin-deletion-time"))
    return _serialize(root)


# --------------------------------------------------------------------------- #
# Parsers
# --------------------------------------------------------------------------- #


def _parse_multistatus(xml: bytes | str) -> etree._Element:
    if isinstance(xml, str):
        xml = xml.encode("utf-8")
    # resolve_entities=False guards against XXE on untrusted server responses.
    parser = etree.XMLParser(resolve_entities=False, no_network=True)
    return etree.fromstring(xml, parser=parser)


def _text(el: etree._Element | None) -> str | None:
    if el is None:
        return None
    return el.text.strip() if el.text else None


def href_to_path(href: str, user: str) -> str:
    """URL-decode an href and strip the ``/remote.php/dav/files/<user>/`` prefix.

    Returns a path relative to the user's WebDAV root (no leading slash).
    """
    decoded = unquote(href)
    prefix = f"/remote.php/dav/files/{user}/"
    idx = decoded.find(prefix)
    if idx != -1:
        decoded = decoded[idx + len(prefix) :]
    else:
        # Fall back to stripping any /remote.php/dav/files/<something>/ prefix.
        marker = "/remote.php/dav/files/"
        pos = decoded.find(marker)
        if pos != -1:
            rest = decoded[pos + len(marker) :]
            decoded = rest.split("/", 1)[1] if "/" in rest else ""
    return decoded.strip("/")


def _is_collection(response: etree._Element) -> bool:
    rtype = response.find(f".//{_qn('d', 'resourcetype')}")
    if rtype is None:
        return False
    return rtype.find(_qn("d", "collection")) is not None


def parse_file_report(xml: bytes | str, user: str) -> list[FileRef]:
    """Parse an ``oc:filter-files`` multistatus into a list of ``FileRef``."""
    root = _parse_multistatus(xml)
    refs: list[FileRef] = []
    for response in root.findall(_qn("d", "response")):
        href_el = response.find(_qn("d", "href"))
        href = _text(href_el)
        if href is None:
            continue
        path = href_to_path(href, user)
        if not path:
            # Skip the collection root itself (the user root has empty path).
            continue
        fileid_el = response.find(f".//{_qn('oc', 'fileid')}")
        fileid_text = _text(fileid_el)
        if fileid_text is None or not fileid_text.isdigit():
            continue
        refs.append(
            FileRef(
                fileid=int(fileid_text),
                path=path,
                is_dir=_is_collection(response),
            )
        )
    return refs


def parse_systemtags(xml: bytes | str) -> list[TagSpec]:
    """Parse a systemtags PROPFIND (or relations PROPFIND) into ``TagSpec`` list."""
    root = _parse_multistatus(xml)
    tags: list[TagSpec] = []
    for response in root.findall(_qn("d", "response")):
        id_el = response.find(f".//{_qn('oc', 'id')}")
        name_el = response.find(f".//{_qn('oc', 'display-name')}")
        id_text = _text(id_el)
        name = _text(name_el)
        if name is None:
            # The collection root response carries no display-name -> skip.
            continue
        tag_id = int(id_text) if id_text and id_text.isdigit() else None
        tags.append(TagSpec(id=tag_id, name=name))
    return tags


def parse_shred_props(xml: bytes | str) -> dict[str, object]:
    """Parse a shred-target PROPFIND (Depth 0) into the guard props.

    Returns a dict with: ``fileid`` (int|None), ``size`` (int|None),
    ``share_types`` (list[int]) and ``mount_type`` (str|None, e.g.
    ``shared``/``group``/``external``; empty/None means a normal local file),
    plus ``is_dir`` (bool). Missing props default to "not shared / not mounted"
    so the caller's refusal logic only triggers on positive evidence.
    """
    root = _parse_multistatus(xml)
    response = root.find(_qn("d", "response"))
    out: dict[str, object] = {
        "fileid": None,
        "size": None,
        "share_types": [],
        "mount_type": None,
        "is_dir": False,
    }
    if response is None:
        return out
    fileid_text = _text(response.find(f".//{_qn('oc', 'fileid')}"))
    if fileid_text and fileid_text.isdigit():
        out["fileid"] = int(fileid_text)
    size_text = _text(response.find(f".//{_qn('oc', 'size')}"))
    if size_text and size_text.isdigit():
        out["size"] = int(size_text)
    mount_type = _text(response.find(f".//{_qn('nc', 'mount-type')}"))
    out["mount_type"] = mount_type or None
    share_types: list[int] = []
    st_el = response.find(f".//{_qn('oc', 'share-types')}")
    if st_el is not None:
        for child in st_el.findall(_qn("oc", "share-type")):
            txt = _text(child)
            if txt and txt.lstrip("-").isdigit():
                share_types.append(int(txt))
    out["share_types"] = share_types
    out["is_dir"] = _is_collection(response)
    return out


def parse_trash_items(xml: bytes | str) -> list[dict[str, object]]:
    """Parse a trashbin PROPFIND into a list of trash-item dicts.

    Each item: ``node_name`` (the trash node id, taken from the href tail — NEVER
    constructed), ``fileid`` (int|None, the STABLE oc:fileid matching the live
    file), ``original_location`` (str|None) and ``deletion_time`` (int|None).
    The collection root response (the ``/trash`` folder itself) is skipped.
    """
    root = _parse_multistatus(xml)
    items: list[dict[str, object]] = []
    for response in root.findall(_qn("d", "response")):
        href = _text(response.find(_qn("d", "href")))
        if href is None:
            continue
        # Node name = last non-empty path segment of the decoded href.
        node = unquote(href).rstrip("/").rsplit("/", 1)[-1]
        if not node or node == "trash":
            # The /trashbin/<user>/trash collection root itself — skip.
            continue
        fileid_text = _text(response.find(f".//{_qn('oc', 'fileid')}"))
        fileid = int(fileid_text) if fileid_text and fileid_text.isdigit() else None
        original = _text(
            response.find(f".//{_qn('nc', 'trashbin-original-location')}")
        )
        del_text = _text(response.find(f".//{_qn('nc', 'trashbin-deletion-time')}"))
        deletion_time = int(del_text) if del_text and del_text.isdigit() else None
        items.append(
            {
                "node_name": node,
                "fileid": fileid,
                "original_location": original,
                "deletion_time": deletion_time,
            }
        )
    return items


def parse_lastmodified(xml: bytes | str) -> datetime | None:
    """Parse ``d:getlastmodified`` (RFC 1123) from a Depth-0 PROPFIND -> aware UTC.

    Returns ``None`` if the prop is absent or unparseable (caller falls back to
    ``now()``). The header date is GMT/UTC; we return a tz-aware UTC datetime.
    """
    root = _parse_multistatus(xml)
    response = root.find(_qn("d", "response"))
    if response is None:
        return None
    text = _text(response.find(f".//{_qn('d', 'getlastmodified')}"))
    if not text:
        return None
    try:
        dt = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def parse_content_location_id(content_location: str) -> int | None:
    """Extract the trailing numeric id from a ``Content-Location`` header.

    e.g. ``/remote.php/dav/systemtags/42`` -> 42.
    """
    if not content_location:
        return None
    tail = content_location.rstrip("/").rsplit("/", 1)[-1]
    return int(tail) if tail.isdigit() else None

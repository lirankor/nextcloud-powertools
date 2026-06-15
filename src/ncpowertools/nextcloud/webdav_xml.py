"""Pure functions to BUILD and PARSE the WebDAV REPORT/PROPFIND XML.

No I/O here — these are deterministic transforms used by ``client.py`` and
exercised directly in unit tests. lxml is used for namespace ergonomics.

Three namespaces are in play everywhere (CONTEXT.md §2):
  DAV:                          -> d
  http://owncloud.org/ns        -> oc
  http://nextcloud.org/ns       -> nc
"""

from __future__ import annotations

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


def parse_content_location_id(content_location: str) -> int | None:
    """Extract the trailing numeric id from a ``Content-Location`` header.

    e.g. ``/remote.php/dav/systemtags/42`` -> 42.
    """
    if not content_location:
        return None
    tail = content_location.rstrip("/").rsplit("/", 1)[-1]
    return int(tail) if tail.isdigit() else None

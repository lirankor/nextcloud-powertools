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
    """REPORT body filtering by ``oc:fileid`` (fileid -> path resolution)."""
    root = _filter_files_root()
    _standard_props(root)
    rules = etree.SubElement(root, _qn("oc", "filter-rules"))
    etree.SubElement(rules, _qn("oc", "fileid")).text = str(fileid)
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

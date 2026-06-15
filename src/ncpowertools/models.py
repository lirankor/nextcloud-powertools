"""Core data models (pydantic v2) shared across the worker.

Kept deliberately small and matching ARCHITECTURE.md's data-model section.
``TagEvent`` is normalized from a webhook payload (M3) OR synthesized by the
poller (M3); it is defined here so the client/tests can reference it.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class TagSpec(BaseModel):
    """A Nextcloud system tag."""

    model_config = ConfigDict(frozen=True)

    id: int | None = None
    name: str


class FileRef(BaseModel):
    """A resolved Nextcloud file or folder.

    ``path`` is relative to the user's WebDAV root (the
    ``/remote.php/dav/files/<user>/`` prefix is stripped) and not percent-encoded.
    """

    fileid: int
    path: str
    is_dir: bool = False
    name: str = ""
    parent: str = ""

    def model_post_init(self, __context: object) -> None:
        # Derive name/parent from path when not supplied, so callers can build
        # a FileRef from just (fileid, path, is_dir).
        clean = self.path.strip("/")
        if not self.name:
            object.__setattr__(self, "name", clean.rsplit("/", 1)[-1] if clean else "")
        if not self.parent and "/" in clean:
            object.__setattr__(self, "parent", clean.rsplit("/", 1)[0])


class TagEvent(BaseModel):
    """A normalized tag-assignment event (from webhook or poller)."""

    uid: str
    fileids: list[int] = Field(default_factory=list)
    tagids: list[int] = Field(default_factory=list)
    raw: dict[str, object] = Field(default_factory=dict)


class ActionResult(BaseModel):
    """The outcome of a handler run (M2)."""

    ok: bool
    outputs: list[str] = Field(default_factory=list)
    message: str = ""

"""Action handler registry (M2).

``ACTIONS`` maps an **action name** to its :class:`~.base.Handler` instance.
:func:`resolve` looks an action up and raises a clear :class:`HandlerError` on
an unknown name.

How tags map onto this (the pipeline, M3, does the wiring):
``Settings.TAG_ACTIONS`` is a ``tag-name -> action-name`` mapping (default:
``extract:extract, zip:zip, rar:rar, render-png:render-png, render:render``).
When a trigger tag fires, the pipeline looks up the tag in ``TAG_ACTIONS`` to
get the action name, then calls ``resolve(action_name)`` to get the handler.
Adding a tag is a ``TAG_ACTIONS`` entry; adding an action is an ``ACTIONS``
entry here; adding a render *source type* is a ``@renderer`` line in
``render.py``.
"""

from __future__ import annotations

from ..errors import HandlerError
from .archives import ExtractHandler
from .base import Handler, HandlerContext
from .compress import RarHandler, SevenZipHandler, ZipHandler
from .render import RenderJpgHandler, RenderPngHandler

ACTIONS: dict[str, Handler] = {
    "extract": ExtractHandler(),
    "zip": ZipHandler(),
    "rar": RarHandler(),
    "7z": SevenZipHandler(),
    "render-png": RenderPngHandler(),
    "render": RenderJpgHandler(),
}


def resolve(action: str) -> Handler:
    """Return the handler for ``action`` or raise :class:`HandlerError`."""
    try:
        return ACTIONS[action]
    except KeyError:
        known = ", ".join(sorted(ACTIONS))
        raise HandlerError(f"unknown action {action!r}; known actions: {known}") from None


__all__ = ["ACTIONS", "resolve", "Handler", "HandlerContext"]

"""Typed exceptions for ncpowertools.

M1 defines the exceptions M1 needs plus stubs that later milestones (M2/M3)
import. They all derive from :class:`NcPowertoolsError` so callers can catch
broadly when needed.
"""

from __future__ import annotations


class NcPowertoolsError(Exception):
    """Base class for all ncpowertools errors."""


class ConfigError(NcPowertoolsError):
    """Raised when configuration is missing or invalid."""


class NcApiError(NcPowertoolsError):
    """Raised on a non-2xx (or otherwise unexpected) Nextcloud API response.

    Carries the HTTP ``status`` and request ``url`` plus an optional body
    ``snippet`` to aid debugging without dumping whole responses into logs.
    """

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        url: str | None = None,
        snippet: str | None = None,
    ) -> None:
        self.status = status
        self.url = url
        self.snippet = snippet
        parts = [message]
        if status is not None:
            parts.append(f"status={status}")
        if url is not None:
            parts.append(f"url={url}")
        if snippet:
            parts.append(f"body={snippet!r}")
        super().__init__(" ".join(parts))


class HandlerError(NcPowertoolsError):
    """Raised when an action handler fails (M2)."""


class UnsafeArchiveError(HandlerError):
    """Raised when an archive member would escape the destination dir (M2)."""


class ArchiveTooLargeError(HandlerError):
    """Raised when an archive exceeds the zip-bomb guards (M2)."""


class RenderError(HandlerError):
    """Raised when a render/convert subprocess fails (M2)."""

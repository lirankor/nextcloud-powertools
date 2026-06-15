"""Handler protocol and shared context for M2 action handlers.

A :class:`Handler` operates **only on local temp files** and returns an
:class:`~ncpowertools.models.ActionResult` carrying the local output paths it
produced. The pipeline (M3) owns download/upload; handlers must never touch the
``NextcloudClient``.

``HandlerContext`` carries everything a handler needs: the work directory, the
resolved :class:`~ncpowertools.models.FileRef` of the source, the zip-bomb
limits, the RAR-enable flag, and a logger. :meth:`HandlerContext.output_dir`
gives the directory a handler should write its outputs into.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from ..models import ActionResult, FileRef


@dataclass
class HandlerContext:
    """Per-run context handed to a handler by the pipeline.

    Attributes
    ----------
    work_dir:
        The scratch directory for this run (e.g. ``WORK_DIR/<fileid>``). Outputs
        are written under here so the pipeline can collect + upload them and then
        delete the whole tree.
    src:
        The resolved source file/folder. Handlers inspect ``src.name`` /
        ``src.is_dir`` etc. but operate on the *local* copy passed to ``run``.
    max_uncompressed_size:
        Zip-bomb guard: cumulative uncompressed bytes must not exceed this.
    max_files:
        Zip-bomb guard: archive member count must not exceed this.
    enable_rar:
        Whether RAR *creation* is permitted (the ``rar`` binary is proprietary
        and opt-in). Extraction via ``unrar`` is always allowed.
    logger:
        Structured logger for the handler to use.
    """

    work_dir: Path
    src: FileRef
    max_uncompressed_size: int
    max_files: int
    enable_rar: bool
    logger: logging.Logger

    def output_dir(self) -> Path:
        """Return (creating if needed) the local directory handlers write into.

        This is ``work_dir/out`` — a stable, isolated subdir so handler outputs
        never collide with the downloaded source the pipeline placed elsewhere.
        """
        out = self.work_dir / "out"
        out.mkdir(parents=True, exist_ok=True)
        return out


@runtime_checkable
class Handler(Protocol):
    """The contract every action handler implements.

    ``name`` is the action name (registry key), e.g. ``"extract"``.
    """

    name: str

    def can_handle(self, src: FileRef) -> bool:
        """Whether this handler can act on ``src`` (e.g. extract: is it an archive?)."""
        ...

    def run(self, ctx: HandlerContext, src_local: Path) -> ActionResult:
        """Process the local ``src_local`` and return an :class:`ActionResult`.

        ``ActionResult.outputs`` are local filesystem paths (as strings) the
        pipeline should upload into the source's parent folder.
        """
        ...

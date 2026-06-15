"""Compression handlers: ``zip`` (stdlib), ``rar`` (opt-in binary), ``7z`` (open alt).

* ``zip`` — compress a file or a folder into ``<name>.zip`` using stdlib
  :mod:`zipfile`, deterministically (sorted entries, fixed timestamps).
* ``rar`` — compress into ``<name>.rar`` via the proprietary ``rar`` binary.
  Disabled unless ``ctx.enable_rar`` is True AND ``rar`` is on PATH; otherwise a
  clear :class:`HandlerError` points the user at 7z.
* ``7z`` — the open alternative the rar error mentions; compresses to
  ``<name>.7z`` via the ``7z`` binary.

Handlers write the archive into ``ctx.output_dir()`` and return its local path.
"""

from __future__ import annotations

import shutil
import subprocess
import zipfile
from pathlib import Path

from ..errors import HandlerError
from ..models import ActionResult, FileRef
from .base import HandlerContext

_SUBPROC_TIMEOUT = 300
# Deterministic zip timestamp (1980-01-01, the zip epoch) for reproducibility.
_ZIP_DATE = (1980, 1, 1, 0, 0, 0)


def _iter_folder(root: Path) -> list[tuple[Path, str]]:
    """Yield (abs_path, arcname) for every file under ``root``, sorted.

    ``arcname`` is prefixed with the folder's own name so the archive unpacks
    into a single top-level directory.
    """
    items: list[tuple[Path, str]] = []
    base = root.name
    for p in sorted(root.rglob("*")):
        if p.is_file():
            rel = p.relative_to(root).as_posix()
            items.append((p, f"{base}/{rel}"))
    return items


class ZipHandler:
    """``zip``: compress a file or folder into ``<name>.zip`` (stdlib)."""

    name = "zip"

    def can_handle(self, src: FileRef) -> bool:
        return True  # any file or folder can be zipped

    def run(self, ctx: HandlerContext, src_local: Path) -> ActionResult:
        out_dir = ctx.output_dir()
        out = out_dir / f"{ctx.src.name}.zip"
        with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            if src_local.is_dir():
                entries = _iter_folder(src_local)
                if not entries:
                    # Preserve an empty folder as a single directory entry.
                    info = zipfile.ZipInfo(f"{src_local.name}/", date_time=_ZIP_DATE)
                    zf.writestr(info, b"")
                for abs_path, arcname in entries:
                    self._write(zf, abs_path, arcname)
            else:
                self._write(zf, src_local, ctx.src.name)
        ctx.logger.info("created zip", extra={"output": out.name})
        return ActionResult(ok=True, outputs=[str(out)], message=f"created {out.name}")

    def _write(self, zf: zipfile.ZipFile, path: Path, arcname: str) -> None:
        info = zipfile.ZipInfo(arcname, date_time=_ZIP_DATE)
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = 0o644 << 16
        with path.open("rb") as f:
            zf.writestr(info, f.read())


class RarHandler:
    """``rar``: compress into ``<name>.rar`` via the proprietary ``rar`` binary."""

    name = "rar"

    def can_handle(self, src: FileRef) -> bool:
        return True

    def run(self, ctx: HandlerContext, src_local: Path) -> ActionResult:
        if not ctx.enable_rar:
            raise HandlerError(
                "RAR creation disabled (ENABLE_RAR=false); use the open 7z alternative"
            )
        binary = shutil.which("rar")
        if binary is None:
            raise HandlerError(
                "rar binary not available; RAR creation needs the proprietary "
                "'rar' tool — use the open 7z alternative"
            )
        out_dir = ctx.output_dir()
        out = out_dir / f"{ctx.src.name}.rar"
        # `a` add; `-ep1` strip the leading path so the archive holds the
        # source's own name at top level; `-o+` overwrite; `-r` recurse folders.
        proc = subprocess.run(  # noqa: S603 - args are controlled
            [binary, "a", "-r", "-ep1", "-o+", str(out), str(src_local)],
            capture_output=True,
            text=True,
            timeout=_SUBPROC_TIMEOUT,
        )
        if proc.returncode != 0:
            raise HandlerError(f"rar creation failed: {_snippet(proc.stderr)}")
        ctx.logger.info("created rar", extra={"output": out.name})
        return ActionResult(ok=True, outputs=[str(out)], message=f"created {out.name}")


class SevenZipHandler:
    """``7z``: the open alternative — compress into ``<name>.7z`` via ``7z``."""

    name = "7z"

    def can_handle(self, src: FileRef) -> bool:
        return True

    def run(self, ctx: HandlerContext, src_local: Path) -> ActionResult:
        binary = shutil.which("7z") or shutil.which("7za")
        if binary is None:
            raise HandlerError("7z binary not available for 7z compression")
        out_dir = ctx.output_dir()
        out = out_dir / f"{ctx.src.name}.7z"
        proc = subprocess.run(  # noqa: S603
            [binary, "a", "-y", str(out), str(src_local)],
            capture_output=True,
            text=True,
            timeout=_SUBPROC_TIMEOUT,
        )
        if proc.returncode != 0:
            raise HandlerError(f"7z creation failed: {_snippet(proc.stderr)}")
        ctx.logger.info("created 7z", extra={"output": out.name})
        return ActionResult(ok=True, outputs=[str(out)], message=f"created {out.name}")


def _snippet(text: str | None, limit: int = 400) -> str:
    if not text:
        return ""
    text = text.strip()
    return text if len(text) <= limit else text[:limit] + "..."

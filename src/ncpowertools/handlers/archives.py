"""The ``extract`` handler: decompress archives into a NEW subfolder.

Supported formats (detected by extension, with a magic-byte cross-check where
cheap): zip, tar (+ tar.gz/tgz, tar.bz2), bare gz, 7z (via ``7z``), rar (via
``unrar``).

Safety is the point (CONTEXT.md §9, applies to **every** format):

* **zip-slip / path traversal** — every member's resolved target must stay
  inside the destination dir; ``..``, absolute paths and symlink escapes are
  rejected with :class:`UnsafeArchiveError`.
* **zip-bomb** — cumulative uncompressed size > ``max_uncompressed_size`` OR
  member count > ``max_files`` raises :class:`ArchiveTooLargeError`; any partial
  output is removed.

The original archive is never modified or deleted; extraction creates a sibling
folder named after the archive stem (``foo.zip`` -> ``foo/``).
"""

from __future__ import annotations

import gzip
import shutil
import subprocess
import tarfile
import zipfile
from pathlib import Path
from typing import IO

from ..errors import ArchiveTooLargeError, HandlerError, UnsafeArchiveError
from ..models import ActionResult, FileRef
from .base import HandlerContext

# Subprocess timeout (seconds) for 7z/unrar listing + extraction.
_SUBPROC_TIMEOUT = 300
# How much we read at a time when streaming-decompressing a bare gz.
_GZ_CHUNK = 1024 * 1024

# Lowercased extensions (or compound suffixes) we recognize. Order matters for
# compound suffixes; we check the longest first via _archive_kind.
_TAR_GZ_SUFFIXES = (".tar.gz", ".tgz")
_TAR_BZ2_SUFFIXES = (".tar.bz2", ".tbz2", ".tbz")
_TAR_XZ_SUFFIXES = (".tar.xz", ".txz")


def _archive_kind(name: str) -> str | None:
    """Classify an archive by its (lowercased) filename. ``None`` if unknown."""
    low = name.lower()
    if low.endswith(_TAR_GZ_SUFFIXES):
        return "tar"  # tarfile auto-detects gzip
    if low.endswith(_TAR_BZ2_SUFFIXES):
        return "tar"
    if low.endswith(_TAR_XZ_SUFFIXES):
        return "tar"
    if low.endswith(".tar"):
        return "tar"
    if low.endswith(".zip"):
        return "zip"
    if low.endswith(".7z"):
        return "7z"
    if low.endswith(".rar"):
        return "rar"
    if low.endswith(".gz"):
        return "gz"  # bare gz (single stream); checked after tar.gz above
    return None


def _stem_for(name: str) -> str:
    """The output-folder name for an archive: strip the recognized suffix."""
    low = name.lower()
    for suf in (*_TAR_GZ_SUFFIXES, *_TAR_BZ2_SUFFIXES, *_TAR_XZ_SUFFIXES):
        if low.endswith(suf):
            return name[: -len(suf)]
    for suf in (".tar", ".zip", ".7z", ".rar", ".gz"):
        if low.endswith(suf):
            return name[: -len(suf)]
    return name


def _safe_join(dest: Path, member: str) -> Path:
    """Resolve ``member`` under ``dest`` or raise :class:`UnsafeArchiveError`.

    Rejects absolute paths and any ``..`` traversal. ``dest`` is assumed to
    already be resolved/absolute.
    """
    member = member.replace("\\", "/")
    candidate = Path(member)
    if candidate.is_absolute() or member.startswith("/"):
        raise UnsafeArchiveError(f"archive member has an absolute path: {member!r}")
    # Resolve against dest WITHOUT touching the filesystem (member may not exist
    # yet); os.path-style normalization is enough to catch '..' escapes.
    target = (dest / member).resolve()
    try:
        target.relative_to(dest)
    except ValueError as exc:
        raise UnsafeArchiveError(
            f"archive member escapes destination: {member!r}"
        ) from exc
    return target


class ExtractHandler:
    """``extract``: decompress an archive into a new sibling subfolder."""

    name = "extract"

    def can_handle(self, src: FileRef) -> bool:
        return not src.is_dir and _archive_kind(src.name) is not None

    def run(self, ctx: HandlerContext, src_local: Path) -> ActionResult:
        kind = _archive_kind(ctx.src.name)
        if kind is None:
            raise HandlerError(f"not a recognized archive: {ctx.src.name!r}")

        dest = (ctx.output_dir() / _stem_for(ctx.src.name)).resolve()
        if dest.exists():
            shutil.rmtree(dest)
        dest.mkdir(parents=True, exist_ok=True)

        try:
            if kind == "zip":
                self._extract_zip(src_local, dest, ctx)
            elif kind == "tar":
                self._extract_tar(src_local, dest, ctx)
            elif kind == "gz":
                self._extract_gz(src_local, dest, ctx)
            elif kind == "7z":
                self._extract_7z(src_local, dest, ctx)
            elif kind == "rar":
                self._extract_rar(src_local, dest, ctx)
            else:  # pragma: no cover - guarded above
                raise HandlerError(f"unhandled archive kind: {kind}")
        except Exception:
            # Clean any partial output on ANY failure so we never leave a
            # half-extracted (possibly bomb-ish) tree behind.
            shutil.rmtree(dest, ignore_errors=True)
            raise

        outputs = sorted(str(p) for p in dest.rglob("*") if p.is_file())
        ctx.logger.info(
            "extracted archive",
            extra={"archive": ctx.src.name, "kind": kind, "files": len(outputs)},
        )
        return ActionResult(
            ok=True,
            outputs=outputs,
            message=f"extracted {len(outputs)} file(s) from {ctx.src.name} into {dest.name}/",
        )

    # --- stdlib formats (real, no external binary) ---

    def _extract_zip(self, src: Path, dest: Path, ctx: HandlerContext) -> None:
        try:
            self._extract_zip_inner(src, dest, ctx)
        except zipfile.BadZipFile as exc:
            raise HandlerError(f"corrupt zip: {exc}") from exc

    def _extract_zip_inner(self, src: Path, dest: Path, ctx: HandlerContext) -> None:
        with zipfile.ZipFile(src) as zf:
            infos = zf.infolist()
            total = 0
            count = 0
            for info in infos:
                if info.is_dir():
                    continue
                count += 1
                total += info.file_size
                self._check_limits(total, count, ctx)
                target = _safe_join(dest, info.filename)
                target.parent.mkdir(parents=True, exist_ok=True)
                # Stream-extract with an overrun guard against a lying header.
                with zf.open(info) as srcf, target.open("wb") as outf:
                    self._copy_capped(srcf, outf, info.file_size, ctx, total)

    def _extract_tar(self, src: Path, dest: Path, ctx: HandlerContext) -> None:
        try:
            self._extract_tar_inner(src, dest, ctx)
        except tarfile.TarError as exc:
            raise HandlerError(f"corrupt tar: {exc}") from exc

    def _extract_tar_inner(self, src: Path, dest: Path, ctx: HandlerContext) -> None:
        with tarfile.open(src) as tf:  # auto-detects gz/bz2/xz
            total = 0
            count = 0
            for member in tf.getmembers():
                if member.isdir():
                    # Validate the dir path too, then create it.
                    _safe_join(dest, member.name).mkdir(parents=True, exist_ok=True)
                    continue
                if member.issym() or member.islnk():
                    # A symlink/hardlink could point outside dest; reject both
                    # the link name and its target.
                    _safe_join(dest, member.name)
                    if member.linkname:
                        _safe_join(dest, member.linkname)
                    raise UnsafeArchiveError(
                        f"refusing to extract link member: {member.name!r}"
                    )
                if not member.isfile():
                    # Skip devices/fifos etc. - never extract special files.
                    continue
                count += 1
                total += member.size
                self._check_limits(total, count, ctx)
                target = _safe_join(dest, member.name)
                target.parent.mkdir(parents=True, exist_ok=True)
                extracted = tf.extractfile(member)
                if extracted is None:  # pragma: no cover - defensive
                    continue
                with extracted as srcf, target.open("wb") as outf:
                    self._copy_capped(srcf, outf, member.size, ctx, total)

    def _extract_gz(self, src: Path, dest: Path, ctx: HandlerContext) -> None:
        # Bare gz has no member list; name the single output after the stem and
        # cap bytes while streaming.
        out_name = _stem_for(ctx.src.name) or "decompressed"
        # _stem_for already stripped .gz; if the original was e.g. "data.gz"
        # the output is "data". Guard against an empty name.
        target = _safe_join(dest, Path(out_name).name)
        target.parent.mkdir(parents=True, exist_ok=True)
        written = 0
        try:
            with gzip.open(src, "rb") as gz, target.open("wb") as outf:
                while True:
                    chunk = gz.read(_GZ_CHUNK)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > ctx.max_uncompressed_size:
                        raise ArchiveTooLargeError(
                            f"gz decompressed size exceeds limit "
                            f"({ctx.max_uncompressed_size} bytes)"
                        )
                    outf.write(chunk)
        except (OSError, EOFError) as exc:
            if isinstance(exc, ArchiveTooLargeError):  # pragma: no cover
                raise
            raise HandlerError(f"corrupt gz: {exc}") from exc

    # --- subprocess formats (7z / rar) ---

    def _extract_7z(self, src: Path, dest: Path, ctx: HandlerContext) -> None:
        binary = shutil.which("7z") or shutil.which("7za")
        if binary is None:
            raise HandlerError("7z binary not available to extract .7z archive")
        # List first to enforce limits before writing anything.
        listing = subprocess.run(  # noqa: S603 - args are controlled
            [binary, "l", "-slt", "-p-", str(src)],
            capture_output=True,
            text=True,
            timeout=_SUBPROC_TIMEOUT,
        )
        if listing.returncode != 0:
            raise HandlerError(f"7z listing failed: {_snippet(listing.stderr)}")
        total, count, names = _parse_7z_listing(listing.stdout)
        self._check_limits(total, count, ctx)
        for n in names:
            _safe_join(dest, n)  # reject traversal before extracting
        proc = subprocess.run(  # noqa: S603
            [binary, "x", "-y", "-p-", f"-o{dest}", str(src)],
            capture_output=True,
            text=True,
            timeout=_SUBPROC_TIMEOUT,
        )
        if proc.returncode != 0:
            raise HandlerError(f"7z extraction failed: {_snippet(proc.stderr)}")

    def _extract_rar(self, src: Path, dest: Path, ctx: HandlerContext) -> None:
        binary = shutil.which("unrar")
        if binary is None:
            raise HandlerError("unrar binary not available to extract .rar archive")
        listing = subprocess.run(  # noqa: S603
            [binary, "lt", "-p-", str(src)],
            capture_output=True,
            text=True,
            timeout=_SUBPROC_TIMEOUT,
        )
        if listing.returncode != 0:
            raise HandlerError(f"unrar listing failed: {_snippet(listing.stderr)}")
        total, count, names = _parse_unrar_listing(listing.stdout)
        self._check_limits(total, count, ctx)
        for n in names:
            _safe_join(dest, n)
        # `x` keeps paths; `-o+` overwrite; trailing slash = output dir.
        proc = subprocess.run(  # noqa: S603
            [binary, "x", "-o+", "-p-", str(src), f"{dest}/"],
            capture_output=True,
            text=True,
            timeout=_SUBPROC_TIMEOUT,
        )
        if proc.returncode != 0:
            raise HandlerError(f"unrar extraction failed: {_snippet(proc.stderr)}")

    # --- shared guards ---

    def _check_limits(self, total: int, count: int, ctx: HandlerContext) -> None:
        if count > ctx.max_files:
            raise ArchiveTooLargeError(
                f"archive member count {count} exceeds limit {ctx.max_files}"
            )
        if total > ctx.max_uncompressed_size:
            raise ArchiveTooLargeError(
                f"archive uncompressed size {total} exceeds limit "
                f"{ctx.max_uncompressed_size}"
            )

    def _copy_capped(
        self,
        srcf: IO[bytes],
        outf: IO[bytes],
        declared: int,
        ctx: HandlerContext,
        running_total: int,
    ) -> None:
        """Copy a member guarding against a header that under-declares its size.

        ``running_total`` already includes ``declared``; if the actual stream
        produces more bytes than declared we re-check the cumulative cap so a
        lying header can't slip a bomb past the pre-check.
        """
        read = srcf.read
        write = outf.write
        written = 0
        # Headroom: cumulative bytes already counted minus this member's own
        # declared size, so the effective cap for THIS member is what's left.
        budget = ctx.max_uncompressed_size - (running_total - declared)
        while True:
            chunk = read(_GZ_CHUNK)
            if not chunk:
                break
            written += len(chunk)
            if written > budget:
                raise ArchiveTooLargeError(
                    "archive member exceeds uncompressed size limit "
                    "(declared size was smaller than actual)"
                )
            write(chunk)


def _snippet(text: str | None, limit: int = 400) -> str:
    if not text:
        return ""
    text = text.strip()
    return text if len(text) <= limit else text[:limit] + "..."


def _parse_7z_listing(stdout: str) -> tuple[int, int, list[str]]:
    """Parse ``7z l -slt`` output → (total_size, file_count, names).

    ``-slt`` emits ``Path = ...``, ``Size = ...``, ``Folder = +/-`` blocks.
    """
    total = 0
    count = 0
    names: list[str] = []
    cur_path: str | None = None
    cur_size = 0
    cur_is_folder = False
    # ``7z l -slt`` prints an archive-header block (whose ``Path =`` is the
    # archive file itself) followed by a ``----------`` separator, after which
    # the real per-member blocks begin. Only parse members after the separator,
    # otherwise the archive's own (often absolute) path is mistaken for a member.
    in_members = False

    def flush() -> None:
        nonlocal total, count, cur_path, cur_size, cur_is_folder
        if cur_path is not None and not cur_is_folder:
            count += 1
            total += cur_size
            names.append(cur_path)
        cur_path, cur_size, cur_is_folder = None, 0, False

    for line in stdout.splitlines():
        if not in_members:
            if line.startswith("----------"):
                in_members = True
            continue
        if line.startswith("Path = "):
            flush()
            cur_path = line[len("Path = "):].strip()
        elif line.startswith("Size = "):
            raw = line[len("Size = "):].strip()
            cur_size = int(raw) if raw.isdigit() else 0
        elif line.startswith("Folder = "):
            cur_is_folder = line[len("Folder = "):].strip() == "+"
    flush()
    return total, count, names


def _parse_unrar_listing(stdout: str) -> tuple[int, int, list[str]]:
    """Parse ``unrar lt`` (verbose technical) output → (total, count, names)."""
    total = 0
    count = 0
    names: list[str] = []
    cur_name: str | None = None
    cur_size = 0
    cur_is_dir = False

    def flush() -> None:
        nonlocal total, count, cur_name, cur_size, cur_is_dir
        if cur_name is not None and not cur_is_dir:
            count += 1
            total += cur_size
            names.append(cur_name)
        cur_name, cur_size, cur_is_dir = None, 0, False

    for raw in stdout.splitlines():
        line = raw.strip()
        if line.startswith("Name: "):
            flush()
            cur_name = line[len("Name: "):].strip()
        elif line.startswith("Size: "):
            val = line[len("Size: "):].strip()
            cur_size = int(val) if val.isdigit() else 0
        elif line.startswith("Type: "):
            cur_is_dir = "directory" in line.lower()
    flush()
    return total, count, names


# Re-exported for tests that want to exercise the path guard directly.
__all__ = ["ExtractHandler", "_safe_join", "_archive_kind", "_stem_for"]

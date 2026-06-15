"""Render/convert handlers backed by an extensible renderer registry.

A :data:`Renderer` is a callable ``render(src, out, fmt, scratch) -> None`` that
performs a **full** conversion of one source file to ``out`` in the target
format (``"png"`` or ``"jpg"``), running whatever subprocess stage(s) it needs
via the shared :func:`_run` helper. ``scratch`` is a writable directory the
renderer may use for intermediate files (it is cleaned by the caller). Renderers
are registered by lowercased **source** extension via the :func:`renderer`
decorator, so adding a new source type (SVG, TIFF, HEIC, AI, …) is a few lines:

    @renderer("svg")
    def _svg(src: Path, out: Path, fmt: str, scratch: Path) -> None:
        binary = magick_binary()
        bg = "none" if fmt == "png" else "white"
        _run([binary, "-background", bg, str(src), str(out)])

Single-stage renderers (PSD, SVG, …) just issue one ImageMagick command. Some
source types need **multiple** stages: camera raws decode via ``dcraw_emu`` to a
temp TIFF (in ``scratch``) which ImageMagick then converts to the final output —
this two-stage form lives behind the same callable contract, so the directory
walk and single-file paths drive raws and PSDs identically.

Two handlers consume the registry:

* ``render-png`` → target **PNG**, preserving alpha (``-background none``).
* ``render``     → target **JPG**, flattened onto white.

PSD ships out of the box (CONTEXT.md §7): ``in.psd[0]`` selects Photoshop's
flattened composite. Camera raws (CR2/CR3/NEF/ARW/DNG/RAF/ORF/RW2/PEF/SRW) ship
via the two-stage libraw pipeline (CONTEXT.md §7, F2).

Directory-level render (F1)
---------------------------
Both handlers also accept a **directory** as the source. ``can_handle`` returns
True for any directory (the per-file walk decides what actually renders) and for
a single file only when its extension is registered. ``run`` then:

* **File source** — render the one file beside its source (output named
  ``<stem>.<fmt>`` directly under ``output_dir()``), unchanged from before.
* **Directory source** — walk the tree recursively; for every file whose
  lowercased extension is in :data:`RENDERERS`, render it to the target format,
  writing the output under ``output_dir()`` at the **same relative path** as the
  source within the tagged dir (``Album/a.psd`` → ``a.png``, ``Album/sub/b.psd``
  → ``sub/b.png``). The pipeline then uploads each output into the tagged dir's
  namespace at the mirrored location. Semantics:

  - Non-registered files are skipped (logged).
  - ``ctx.max_files`` is a **hard cap on the NUMBER of files rendered**: if more
    renderable files than the cap are found we raise :class:`HandlerError`
    (nothing is uploaded) rather than silently truncating.
  - A directory with **zero** renderable files is a success:
    ``ActionResult(ok=True, outputs=[], message="nothing to render")`` — not an
    error (the pipeline still removes the trigger tag).
  - If an **individual** file fails to render we log it and continue the batch;
    but if **every** attempted render fails we raise :class:`RenderError` so the
    failure surfaces (trigger tag kept, retriable).
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

from ..errors import HandlerError, RenderError
from ..models import ActionResult, FileRef
from .base import HandlerContext

_SUBPROC_TIMEOUT = 300

# A Renderer performs the full conversion of `src` to `out` in `fmt`
# ("png" or "jpg"), using `scratch` (a writable dir the caller cleans) for any
# intermediate files. It runs subprocess stage(s) via `_run` and raises
# RenderError on failure. Returns None (the output is the file it writes).
Renderer = Callable[[Path, Path, str, Path], None]

RENDERERS: dict[str, Renderer] = {}

# Camera-raw source extensions, decoded via the two-stage libraw pipeline.
# Start with the common set; extend by adding to this tuple.
RAW_EXTS: tuple[str, ...] = (
    "cr2",
    "cr3",
    "nef",
    "arw",
    "dng",
    "raf",
    "orf",
    "rw2",
    "pef",
    "srw",
)


def renderer(*exts: str) -> Callable[[Renderer], Renderer]:
    """Register a :data:`Renderer` for one or more lowercased source extensions."""

    def deco(fn: Renderer) -> Renderer:
        for ext in exts:
            RENDERERS[ext.lower().lstrip(".")] = fn
        return fn

    return deco


def magick_binary() -> str:
    """Return the ImageMagick CLI to use (``magick`` preferred, else ``convert``).

    Raises :class:`RenderError` if neither is on PATH.
    """
    binary = shutil.which("magick") or shutil.which("convert")
    if binary is None:
        raise RenderError("ImageMagick not available (neither 'magick' nor 'convert' on PATH)")
    return binary


def _run(argv: list[str]) -> None:
    """Run one conversion stage; raise :class:`RenderError` (with stderr) on failure.

    Shared by every renderer so multi-stage pipelines (raw → TIFF → image) and
    single-stage ones (PSD, SVG) report failures uniformly.
    """
    proc = subprocess.run(  # noqa: S603 - argv built from controlled parts
        argv,
        capture_output=True,
        text=True,
        timeout=_SUBPROC_TIMEOUT,
    )
    if proc.returncode != 0:
        raise RenderError(f"command failed ({Path(argv[0]).name}): {_snippet(proc.stderr)}")


@renderer("psd")
def _render_psd(src: Path, out: Path, fmt: str, scratch: Path) -> None:
    """PSD → PNG/JPG using the embedded flattened composite ``[0]`` (one stage)."""
    binary = magick_binary()
    composite = f"{src}[0]"
    if fmt == "png":
        # Preserve transparency.
        _run([binary, composite, "-background", "none", str(out)])
        return
    # JPG: flatten onto a white matte, decent quality.
    _run(
        [
            binary,
            composite,
            "-background",
            "white",
            "-flatten",
            "-quality",
            "90",
            str(out),
        ]
    )


@renderer(*RAW_EXTS)
def _render_raw(src: Path, out: Path, fmt: str, scratch: Path) -> None:
    """Camera RAW → PNG/JPG via a two-stage libraw pipeline.

    Stage 1: ``dcraw_emu`` decodes the raw to a temp TIFF in ``scratch`` (the
    explicit ``-Z`` output path is CRITICAL — without it dcraw_emu writes
    ``<input>.tiff`` *beside the source*, which breaks read-only source dirs).
    Stage 2: ImageMagick converts the TIFF to the final output.

    This bypasses ImageMagick's policy.xml entirely (IM only ever opens the
    decoded TIFF — TIFF/JPEG/PNG read/write are already allowed), so no raw
    coder needs unlocking. The temp TIFF is always cleaned, even on failure.
    """
    decoder = shutil.which("dcraw_emu")
    if decoder is None:
        raise RenderError("raw decoder not available ('dcraw_emu' not on PATH; install libraw-bin)")
    binary = magick_binary()

    tiff = scratch / f"{src.stem}.decoded.tiff"
    try:
        # Stage 1: decode raw → TIFF (camera WB, sRGB, AHD demosaic, explicit -Z).
        _run(
            [
                decoder,
                "-w",
                "-o",
                "1",
                "-q",
                "3",
                "-T",
                "-Z",
                str(tiff),
                str(src),
            ]
        )
        if not tiff.exists():
            raise RenderError(f"raw decode produced no TIFF for {src.name}")
        # Stage 2: TIFF → final image.
        argv = [
            binary,
            str(tiff),
            "-auto-orient",
            "-colorspace",
            "sRGB",
            "-depth",
            "8",
        ]
        if fmt == "jpg":
            argv += ["-quality", "90"]
        argv.append(str(out))
        _run(argv)
    finally:
        tiff.unlink(missing_ok=True)


def _src_ext(name: str) -> str:
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def resolve_renderer(ext: str) -> Renderer:
    """Look up a renderer by source extension or raise listing supported exts."""
    ext = ext.lower().lstrip(".")
    try:
        return RENDERERS[ext]
    except KeyError:
        supported = ", ".join(sorted(RENDERERS)) or "(none registered)"
        raise RenderError(
            f"no renderer for source extension {ext!r}; supported: {supported}"
        ) from None


class _RenderHandler:
    """Shared render handler; ``target_fmt`` differentiates PNG vs JPG."""

    def __init__(self, name: str, target_fmt: str) -> None:
        self.name = name
        self.target_fmt = target_fmt

    def can_handle(self, src: FileRef) -> bool:
        # A directory always passes — the walk decides what actually renders.
        # A single file passes only when its extension is registered.
        return src.is_dir or _src_ext(src.name) in RENDERERS

    def run(self, ctx: HandlerContext, src_local: Path) -> ActionResult:
        if src_local.is_dir():
            return self._run_dir(ctx, src_local)
        return self._run_file(ctx, src_local)

    # ----------------------------------------------------------------- #
    # single file (unchanged behavior): output beside the source
    # ----------------------------------------------------------------- #

    def _run_file(self, ctx: HandlerContext, src_local: Path) -> ActionResult:
        ext = _src_ext(ctx.src.name)
        out_dir = ctx.output_dir()
        stem = ctx.src.name.rsplit(".", 1)[0] if "." in ctx.src.name else ctx.src.name
        out = out_dir / f"{stem}.{self.target_fmt}"
        self._render_one(ctx, ext, src_local, out)
        ctx.logger.info(
            "rendered file",
            extra={"src": ctx.src.name, "target": self.target_fmt, "output": out.name},
        )
        return ActionResult(ok=True, outputs=[str(out)], message=f"rendered {out.name}")

    # ----------------------------------------------------------------- #
    # directory walk (F1): render every registered file, tree mirrored
    # ----------------------------------------------------------------- #

    def _run_dir(self, ctx: HandlerContext, src_dir: Path) -> ActionResult:
        out_dir = ctx.output_dir()
        # Deterministic order so output/logs are stable across runs.
        candidates = sorted(
            p for p in src_dir.rglob("*") if p.is_file() and _src_ext(p.name) in RENDERERS
        )
        if not candidates:
            ctx.logger.info("nothing to render", extra={"dir": ctx.src.name})
            return ActionResult(ok=True, outputs=[], message="nothing to render")

        # max_files is a HARD cap on the number of renders we'll perform.
        if len(candidates) > ctx.max_files:
            raise HandlerError(
                f"{len(candidates)} renderable files exceed MAX_FILES cap "
                f"({ctx.max_files}); aborting (raise MAX_FILES to allow)"
            )

        outputs: list[str] = []
        failures = 0
        total = len(candidates)
        for src_file in candidates:
            rel = src_file.relative_to(src_dir)
            ext = _src_ext(src_file.name)
            stem = src_file.name.rsplit(".", 1)[0] if "." in src_file.name else src_file.name
            out = out_dir / rel.parent / f"{stem}.{self.target_fmt}"
            out.parent.mkdir(parents=True, exist_ok=True)
            try:
                self._render_one(ctx, ext, src_file, out)
            except RenderError as exc:
                failures += 1
                ctx.logger.warning(
                    "render failed for file (continuing)",
                    extra={"file": rel.as_posix(), "error": str(exc)},
                )
                continue
            outputs.append(str(out))
            ctx.logger.info(
                "rendered file",
                extra={"file": rel.as_posix(), "target": self.target_fmt},
            )

        if not outputs and failures:
            # Every attempted render failed — surface the failure (retriable).
            raise RenderError(
                f"all {failures} render(s) failed under {ctx.src.name}"
            )

        ctx.logger.info(
            "rendered directory",
            extra={"dir": ctx.src.name, "rendered": len(outputs), "found": total},
        )
        return ActionResult(
            ok=True,
            outputs=outputs,
            message=f"rendered {len(outputs)} of {total} files",
        )

    # ----------------------------------------------------------------- #
    # shared single-render primitive
    # ----------------------------------------------------------------- #

    def _render_one(self, ctx: HandlerContext, ext: str, src_file: Path, out: Path) -> None:
        render = resolve_renderer(ext)
        scratch = ctx.work_dir / "scratch"
        scratch.mkdir(parents=True, exist_ok=True)
        try:
            render(src_file, out, self.target_fmt, scratch)
        except RenderError:
            raise
        except subprocess.TimeoutExpired as exc:
            raise RenderError(f"render timed out ({ext}->{self.target_fmt}): {exc}") from exc
        if not out.exists():
            raise RenderError(
                f"render produced no output ({ext}->{self.target_fmt})"
            )


class RenderPngHandler(_RenderHandler):
    """``render-png``: render the source to PNG (alpha preserved)."""

    def __init__(self) -> None:
        super().__init__("render-png", "png")


class RenderJpgHandler(_RenderHandler):
    """``render``: render the source to JPG (flattened onto white)."""

    def __init__(self) -> None:
        super().__init__("render", "jpg")


def _snippet(text: str | None, limit: int = 400) -> str:
    if not text:
        return ""
    text = text.strip()
    return text if len(text) <= limit else text[:limit] + "..."

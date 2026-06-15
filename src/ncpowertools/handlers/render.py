"""Render/convert handlers backed by an extensible renderer registry.

A :class:`Renderer` maps ``(src_path, target_fmt)`` to a subprocess argv list
that drives ImageMagick (``magick`` if present, else IM6 ``convert``). Renderers
are registered by lowercased **source** extension via the :func:`renderer`
decorator, so adding a new source type (SVG, TIFF, HEIC, AI, …) is a few lines:

    @renderer("svg")
    def _svg(src: Path, out: Path, fmt: str) -> list[str]:
        return [magick_binary(), str(src), str(out)]

Two handlers consume the registry:

* ``render-png`` → target **PNG**, preserving alpha (``-background none``).
* ``render``     → target **JPG**, flattened onto white.

PSD ships out of the box (CONTEXT.md §7): ``in.psd[0]`` selects Photoshop's
flattened composite.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

from ..errors import RenderError
from ..models import ActionResult, FileRef
from .base import HandlerContext

_SUBPROC_TIMEOUT = 300

# A Renderer: (src, out, target_fmt) -> argv. target_fmt is "png" or "jpg".
Renderer = Callable[[Path, Path, str], list[str]]

RENDERERS: dict[str, Renderer] = {}


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


@renderer("psd")
def _render_psd(src: Path, out: Path, fmt: str) -> list[str]:
    """PSD → PNG/JPG using the embedded flattened composite ``[0]``."""
    binary = magick_binary()
    composite = f"{src}[0]"
    if fmt == "png":
        # Preserve transparency.
        return [binary, composite, "-background", "none", str(out)]
    # JPG: flatten onto a white matte, decent quality.
    return [
        binary,
        composite,
        "-background",
        "white",
        "-flatten",
        "-quality",
        "90",
        str(out),
    ]


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
        return not src.is_dir and _src_ext(src.name) in RENDERERS

    def run(self, ctx: HandlerContext, src_local: Path) -> ActionResult:
        ext = _src_ext(ctx.src.name)
        render = resolve_renderer(ext)
        out_dir = ctx.output_dir()
        stem = ctx.src.name.rsplit(".", 1)[0] if "." in ctx.src.name else ctx.src.name
        out = out_dir / f"{stem}.{self.target_fmt}"
        argv = render(src_local, out, self.target_fmt)
        proc = subprocess.run(  # noqa: S603 - argv built from controlled parts
            argv,
            capture_output=True,
            text=True,
            timeout=_SUBPROC_TIMEOUT,
        )
        if proc.returncode != 0:
            raise RenderError(f"render failed ({ext}->{self.target_fmt}): {_snippet(proc.stderr)}")
        if not out.exists():
            raise RenderError(
                f"render produced no output ({ext}->{self.target_fmt}); "
                f"stderr: {_snippet(proc.stderr)}"
            )
        ctx.logger.info(
            "rendered file",
            extra={"src": ctx.src.name, "target": self.target_fmt, "output": out.name},
        )
        return ActionResult(ok=True, outputs=[str(out)], message=f"rendered {out.name}")


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

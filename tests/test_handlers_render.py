"""Tests for the render registry + handlers: argv shape, extensibility, real PSD render."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from ncpowertools.errors import RenderError
from ncpowertools.handlers import resolve
from ncpowertools.handlers.base import HandlerContext
from ncpowertools.handlers.render import (
    RENDERERS,
    RenderJpgHandler,
    RenderPngHandler,
    renderer,
    resolve_renderer,
)

CtxFactory = Callable[..., HandlerContext]


# --- registry / extensibility -------------------------------------------------


def test_psd_renderer_registered() -> None:
    assert "psd" in RENDERERS


def test_unknown_ext_raises_listing_supported() -> None:
    with pytest.raises(RenderError, match="supported:"):
        resolve_renderer("xyz")


def test_registry_extensible_a_few_lines() -> None:
    """Adding a source type is a decorator + a function (the extensibility bullet)."""

    @renderer("dummyext")
    def _dummy(src: Path, out: Path, fmt: str) -> list[str]:
        return ["true", str(src), str(out)]

    try:
        assert resolve_renderer("dummyext") is _dummy
        assert resolve_renderer(".DUMMYEXT") is _dummy  # case + dot insensitive
    finally:
        RENDERERS.pop("dummyext", None)


# --- PSD argv shape (mocked subprocess) ---------------------------------------


def _patch_magick(monkeypatch: pytest.MonkeyPatch, binary: str = "/usr/bin/magick") -> None:
    monkeypatch.setattr(
        shutil, "which", lambda name: binary if name in ("magick", "convert") else None
    )


def test_render_png_argv(make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_magick(monkeypatch)
    ctx = make_ctx("art.psd")
    src = ctx.work_dir / "art.psd"
    src.write_bytes(b"8BPS")

    captured: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured.append(argv)
        out = argv[-1]
        Path(out).write_bytes(b"\x89PNG")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = RenderPngHandler().run(ctx, src)
    assert result.ok
    argv = captured[0]
    assert argv[0] == "/usr/bin/magick"
    assert argv[1] == f"{src}[0]"
    assert argv[2:4] == ["-background", "none"]
    assert argv[-1].endswith("art.png")


def test_render_jpg_argv(make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_magick(monkeypatch)
    ctx = make_ctx("art.psd")
    src = ctx.work_dir / "art.psd"
    src.write_bytes(b"8BPS")

    captured: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured.append(argv)
        Path(argv[-1]).write_bytes(b"\xff\xd8\xff")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = RenderJpgHandler().run(ctx, src)
    assert result.ok
    argv = captured[0]
    assert argv[1] == f"{src}[0]"
    assert "-background" in argv and "white" in argv and "-flatten" in argv
    assert "-quality" in argv and "90" in argv
    assert argv[-1].endswith("art.jpg")


def test_render_uses_convert_when_no_magick(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        shutil, "which", lambda name: "/usr/bin/convert" if name == "convert" else None
    )
    ctx = make_ctx("a.psd")
    src = ctx.work_dir / "a.psd"
    src.write_bytes(b"8BPS")

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        Path(argv[-1]).write_bytes(b"\x89PNG")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = RenderPngHandler().run(ctx, src)
    assert result.ok


def test_render_no_imagemagick_raises(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None)
    ctx = make_ctx("a.psd")
    src = ctx.work_dir / "a.psd"
    src.write_bytes(b"8BPS")
    with pytest.raises(RenderError, match="ImageMagick not available"):
        RenderPngHandler().run(ctx, src)


def test_render_unknown_source_ext_raises(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_magick(monkeypatch)
    ctx = make_ctx("photo.heic")
    src = ctx.work_dir / "photo.heic"
    src.write_bytes(b"\x00")
    with pytest.raises(RenderError, match="no renderer for source extension"):
        RenderPngHandler().run(ctx, src)


def test_render_subprocess_failure_raises(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_magick(monkeypatch)
    ctx = make_ctx("a.psd")
    src = ctx.work_dir / "a.psd"
    src.write_bytes(b"8BPS")

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 1, "", "convert: bad coder")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(RenderError, match="render failed"):
        RenderPngHandler().run(ctx, src)


def test_render_handlers_via_registry() -> None:
    assert resolve("render-png").name == "render-png"
    assert resolve("render").name == "render"


def test_can_handle_checks_extension(make_ctx: CtxFactory) -> None:
    handler = RenderPngHandler()
    from ncpowertools.models import FileRef

    assert handler.can_handle(FileRef(fileid=1, path="x.psd", name="x.psd"))
    assert not handler.can_handle(FileRef(fileid=1, path="x.txt", name="x.txt"))


# --- real render (skipped on hosts without ImageMagick) -----------------------


def _make_psd(path: Path) -> bool:
    """Create a tiny PSD via ImageMagick. Returns False if it can't."""
    binary = shutil.which("magick") or shutil.which("convert")
    if binary is None:
        return False
    res = subprocess.run(
        [binary, "-size", "8x8", "xc:none", str(path)], capture_output=True
    )
    return res.returncode == 0 and path.exists()


@pytest.mark.skipif(
    shutil.which("magick") is None and shutil.which("convert") is None,
    reason="ImageMagick not available",
)
def test_render_psd_to_png_real_has_alpha(make_ctx: CtxFactory) -> None:
    ctx = make_ctx("real.psd")
    src = ctx.work_dir / "real.psd"
    if not _make_psd(src):
        pytest.skip("could not create a test PSD")

    result = RenderPngHandler().run(ctx, src)
    out = Path(result.outputs[0])
    assert out.exists()
    # Assert the PNG has an alpha channel.
    binary = shutil.which("magick") or shutil.which("convert")
    probe = subprocess.run(
        [binary, "identify", "-format", "%[channels]", str(out)]
        if binary and binary.endswith("magick")
        else [binary, str(out), "-format", "%[channels]", "info:"],
        capture_output=True,
        text=True,
    )
    assert "a" in probe.stdout.lower()  # rgba / graya etc.

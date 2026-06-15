"""Tests for the render registry + handlers: argv shape, extensibility, real PSD render."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from ncpowertools.errors import HandlerError, RenderError
from ncpowertools.handlers import resolve
from ncpowertools.handlers.base import HandlerContext
from ncpowertools.handlers.render import (
    AFFINITY_EXTS,
    RASTER_EXTS,
    RAW_EXTS,
    RENDERERS,
    VECTOR_PAGE_EXTS,
    RenderJpgHandler,
    RenderPngHandler,
    _carve_largest_png,
    renderer,
    resolve_renderer,
)
from ncpowertools.models import FileRef

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
    def _dummy(src: Path, out: Path, fmt: str, scratch: Path) -> None:
        out.write_bytes(b"x")

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
    ctx = make_ctx("mystery.zzz")
    src = ctx.work_dir / "mystery.zzz"
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
    with pytest.raises(RenderError, match="command failed"):
        RenderPngHandler().run(ctx, src)


def test_render_handlers_via_registry() -> None:
    assert resolve("render-png").name == "render-png"
    assert resolve("render").name == "render"


def test_can_handle_checks_extension(make_ctx: CtxFactory) -> None:
    handler = RenderPngHandler()
    assert handler.can_handle(FileRef(fileid=1, path="x.psd", name="x.psd"))
    assert not handler.can_handle(FileRef(fileid=1, path="x.txt", name="x.txt"))


def test_can_handle_accepts_any_directory(make_ctx: CtxFactory) -> None:
    """A dir always passes can_handle — the walk decides per file (F1)."""
    handler = RenderPngHandler()
    # Even a dir whose own name has no registered ext should pass.
    assert handler.can_handle(FileRef(fileid=1, path="Album", is_dir=True, name="Album"))
    assert handler.can_handle(
        FileRef(fileid=1, path="x.txt", is_dir=True, name="x.txt")
    )


# --- directory walk (F1) -------------------------------------------------------


def _seed_tree(root: Path) -> None:
    """Create a 2-level tree: a.psd, sub/b.psd, notes.txt, sub/cover.jpg(non-reg)."""
    (root / "a.psd").write_bytes(b"8BPS")
    (root / "sub").mkdir(parents=True, exist_ok=True)
    (root / "sub" / "b.psd").write_bytes(b"8BPS")
    (root / "notes.txt").write_text("hi")
    (root / "sub" / "cover.jpg").write_bytes(b"\xff\xd8\xff")  # jpg not registered


def _fake_render(
    monkeypatch: pytest.MonkeyPatch, magic_bytes: bytes = b"\x89PNG"
) -> list[list[str]]:
    """Patch magick + subprocess.run; record argv per call; write output files."""
    _patch_magick(monkeypatch)
    captured: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured.append(argv)
        Path(argv[-1]).write_bytes(magic_bytes)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    return captured


def test_dir_walk_renders_only_registered_exts_tree_preserved(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = _fake_render(monkeypatch)
    ctx = make_ctx("Album", is_dir=True)
    src_dir = ctx.work_dir / "src" / "Album"
    src_dir.mkdir(parents=True)
    _seed_tree(src_dir)

    result = RenderPngHandler().run(ctx, src_dir)
    assert result.ok
    out_root = ctx.output_dir()
    rels = sorted(Path(o).relative_to(out_root).as_posix() for o in result.outputs)
    # only the two PSDs rendered; tree preserved; txt + jpg skipped
    assert rels == ["a.png", "sub/b.png"]
    assert result.message == "rendered 2 of 2 files"
    # per-file argv carries the [0] selector + -background none flags
    for argv in captured:
        assert argv[1].endswith("[0]")
        assert "-background" in argv and "none" in argv


def test_dir_walk_jpg_target(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_render(monkeypatch, magic_bytes=b"\xff\xd8\xff")
    ctx = make_ctx("Album", is_dir=True)
    src_dir = ctx.work_dir / "src" / "Album"
    src_dir.mkdir(parents=True)
    _seed_tree(src_dir)

    result = RenderJpgHandler().run(ctx, src_dir)
    out_root = ctx.output_dir()
    rels = sorted(Path(o).relative_to(out_root).as_posix() for o in result.outputs)
    assert rels == ["a.jpg", "sub/b.jpg"]


def test_dir_walk_max_files_cap_enforced(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_render(monkeypatch)
    ctx = make_ctx("Album", is_dir=True, max_files=1)  # only 1 render allowed
    src_dir = ctx.work_dir / "src" / "Album"
    src_dir.mkdir(parents=True)
    _seed_tree(src_dir)  # two PSDs > cap

    with pytest.raises(HandlerError, match="MAX_FILES"):
        RenderPngHandler().run(ctx, src_dir)


def test_dir_walk_empty_or_no_match_is_success(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_render(monkeypatch)
    ctx = make_ctx("Album", is_dir=True)
    src_dir = ctx.work_dir / "src" / "Album"
    (src_dir / "sub").mkdir(parents=True)
    (src_dir / "notes.txt").write_text("hi")  # nothing renderable

    result = RenderPngHandler().run(ctx, src_dir)
    assert result.ok
    assert result.outputs == []
    assert result.message == "nothing to render"


def test_dir_walk_continues_on_single_failure(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_magick(monkeypatch)
    ctx = make_ctx("Album", is_dir=True)
    src_dir = ctx.work_dir / "src" / "Album"
    src_dir.mkdir(parents=True)
    (src_dir / "good.psd").write_bytes(b"8BPS")
    (src_dir / "bad.psd").write_bytes(b"8BPS")

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if "bad.psd" in argv[1]:
            return subprocess.CompletedProcess(argv, 1, "", "convert: boom")
        Path(argv[-1]).write_bytes(b"\x89PNG")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = RenderPngHandler().run(ctx, src_dir)
    # one failed, one succeeded — batch continues, 1 of 2 rendered
    assert result.ok
    rels = [Path(o).name for o in result.outputs]
    assert rels == ["good.png"]
    assert result.message == "rendered 1 of 2 files"


def test_dir_walk_all_failures_raises(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_magick(monkeypatch)
    ctx = make_ctx("Album", is_dir=True)
    src_dir = ctx.work_dir / "src" / "Album"
    src_dir.mkdir(parents=True)
    (src_dir / "a.psd").write_bytes(b"8BPS")
    (src_dir / "b.psd").write_bytes(b"8BPS")

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 1, "", "convert: boom")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(RenderError, match="all .* render"):
        RenderPngHandler().run(ctx, src_dir)


def test_dir_walk_renders_a_dummy_registered_ext(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Register a new source ext → a dir containing it renders (extensibility)."""

    from ncpowertools.handlers import render as render_mod

    @renderer("foobar")
    def _foo(src: Path, out: Path, fmt: str, scratch: Path) -> None:
        render_mod._run(["true", str(src), str(out)])
        out.write_bytes(b"x")

    captured: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    try:
        ctx = make_ctx("Album", is_dir=True)
        src_dir = ctx.work_dir / "src" / "Album"
        src_dir.mkdir(parents=True)
        (src_dir / "thing.foobar").write_bytes(b"\x00")
        (src_dir / "skip.txt").write_text("no")
        result = RenderPngHandler().run(ctx, src_dir)
        rels = [Path(o).name for o in result.outputs]
        assert rels == ["thing.png"]
        assert captured[0][0] == "true"
    finally:
        RENDERERS.pop("foobar", None)


# --- camera RAW two-stage pipeline (F2) ---------------------------------------


def test_raw_exts_all_registered() -> None:
    """Every camera-raw ext routes through a renderer (registry test)."""
    for ext in RAW_EXTS:
        assert ext in RENDERERS
    # cr2 + a couple others are present (sanity).
    assert {"cr2", "cr3", "nef", "arw", "dng"} <= set(RENDERERS)


def _patch_raw_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend dcraw_emu + convert are both on PATH."""

    def which(name: str) -> str | None:
        if name == "dcraw_emu":
            return "/usr/bin/dcraw_emu"
        if name in ("magick", "convert"):
            return "/usr/bin/convert"
        return None

    monkeypatch.setattr(shutil, "which", which)


def _raw_capture(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Patch tools + subprocess; record argv; emulate dcraw writing the -Z TIFF
    and convert writing its final output."""
    _patch_raw_tools(monkeypatch)
    captured: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured.append(list(argv))
        binary = Path(argv[0]).name
        if binary == "dcraw_emu":
            # honour the explicit -Z output path
            zi = argv.index("-Z")
            Path(argv[zi + 1]).write_bytes(b"II*\x00")  # little-endian TIFF magic
        else:
            Path(argv[-1]).write_bytes(b"\xff\xd8\xff")  # final image
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    return captured


def test_raw_jpg_two_stage_commands(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = _raw_capture(monkeypatch)
    ctx = make_ctx("shot.cr2")
    src = ctx.work_dir / "shot.cr2"
    src.write_bytes(b"\x00raw")

    result = RenderJpgHandler().run(ctx, src)
    assert result.ok
    assert len(captured) == 2  # exactly two stages, in order

    # Stage 1: dcraw_emu with the exact flags, -Z to a scratch path (not beside src).
    stage1 = captured[0]
    assert Path(stage1[0]).name == "dcraw_emu"
    assert stage1[1:8] == ["-w", "-o", "1", "-q", "3", "-T", "-Z"]
    tiff_path = Path(stage1[8])
    assert stage1[9] == str(src)  # source is the LAST arg
    # TIFF lives under the handler scratch dir, NOT beside the source.
    assert "scratch" in tiff_path.parts
    assert tiff_path.parent != src.parent
    assert tiff_path.suffix == ".tiff"

    # Stage 2: convert TIFF → jpg with -auto-orient -colorspace sRGB -depth 8 -quality 90.
    stage2 = captured[1]
    assert Path(stage2[0]).name == "convert"
    assert stage2[1] == str(tiff_path)
    assert "-auto-orient" in stage2
    assert stage2[stage2.index("-colorspace") + 1] == "sRGB"
    assert stage2[stage2.index("-depth") + 1] == "8"
    assert stage2[stage2.index("-quality") + 1] == "90"
    assert stage2[-1].endswith("shot.jpg")

    # The temp TIFF was created under scratch and then cleaned.
    assert not tiff_path.exists()


def test_raw_png_two_stage_commands(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = _raw_capture(monkeypatch)
    ctx = make_ctx("shot.nef")
    src = ctx.work_dir / "shot.nef"
    src.write_bytes(b"\x00raw")

    result = RenderPngHandler().run(ctx, src)
    assert result.ok
    assert len(captured) == 2

    stage1 = captured[0]
    assert Path(stage1[0]).name == "dcraw_emu"
    assert stage1[1:8] == ["-w", "-o", "1", "-q", "3", "-T", "-Z"]
    tiff_path = Path(stage1[8])
    assert "scratch" in tiff_path.parts

    stage2 = captured[1]
    assert Path(stage2[0]).name == "convert"
    assert "-auto-orient" in stage2
    assert stage2[stage2.index("-colorspace") + 1] == "sRGB"
    assert stage2[stage2.index("-depth") + 1] == "8"
    # PNG path does NOT pass -quality (that's JPG-only).
    assert "-quality" not in stage2
    assert stage2[-1].endswith("shot.png")
    assert not tiff_path.exists()


def test_raw_no_decoder_raises(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    # magick present but dcraw_emu absent.
    monkeypatch.setattr(
        shutil, "which", lambda name: "/usr/bin/convert" if name == "convert" else None
    )
    ctx = make_ctx("shot.cr2")
    src = ctx.work_dir / "shot.cr2"
    src.write_bytes(b"\x00raw")
    with pytest.raises(RenderError, match="raw decoder not available"):
        RenderJpgHandler().run(ctx, src)


def test_raw_tiff_cleaned_on_convert_failure(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If stage-2 convert fails, the scratch TIFF is still cleaned up."""
    _patch_raw_tools(monkeypatch)
    seen_tiff: dict[str, Path] = {}

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if Path(argv[0]).name == "dcraw_emu":
            zi = argv.index("-Z")
            tiff = Path(argv[zi + 1])
            tiff.write_bytes(b"II*\x00")
            seen_tiff["p"] = tiff
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.CompletedProcess(argv, 1, "", "convert: boom")

    monkeypatch.setattr(subprocess, "run", fake_run)
    ctx = make_ctx("shot.cr2")
    src = ctx.work_dir / "shot.cr2"
    src.write_bytes(b"\x00raw")
    with pytest.raises(RenderError, match="command failed"):
        RenderJpgHandler().run(ctx, src)
    assert not seen_tiff["p"].exists()  # TIFF cleaned even on failure


def test_raw_routes_through_registry(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A registered raw ext resolves to the raw renderer (same fn for all raws)."""
    assert resolve_renderer("cr2") is resolve_renderer("dng")
    assert resolve_renderer("arw") is resolve_renderer("CR3")  # case-insensitive


def test_dir_walk_mixed_raw_and_psd_skips_txt(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F1 composition: a folder with a.cr2 + b.psd + notes.txt renders both, skips txt."""
    _patch_raw_tools(monkeypatch)

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        binary = Path(argv[0]).name
        if binary == "dcraw_emu":
            zi = argv.index("-Z")
            Path(argv[zi + 1]).write_bytes(b"II*\x00")
        else:
            Path(argv[-1]).write_bytes(b"\xff\xd8\xff")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    ctx = make_ctx("Album", is_dir=True)
    src_dir = ctx.work_dir / "src" / "Album"
    src_dir.mkdir(parents=True)
    (src_dir / "a.cr2").write_bytes(b"\x00raw")
    (src_dir / "b.psd").write_bytes(b"8BPS")
    (src_dir / "notes.txt").write_text("hi")

    result = RenderJpgHandler().run(ctx, src_dir)
    assert result.ok
    out_root = ctx.output_dir()
    rels = sorted(Path(o).relative_to(out_root).as_posix() for o in result.outputs)
    assert rels == ["a.jpg", "b.jpg"]  # raw + psd both rendered; txt skipped
    assert result.message == "rendered 2 of 2 files"


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


@pytest.mark.skipif(
    shutil.which("magick") is None and shutil.which("convert") is None,
    reason="ImageMagick not available",
)
def test_render_dir_two_level_psd_tree_real(make_ctx: CtxFactory) -> None:
    """Real ImageMagick render of a 2-level PSD tree (F1 dir walk)."""
    ctx = make_ctx("Album", is_dir=True)
    src_dir = ctx.work_dir / "src" / "Album"
    (src_dir / "sub").mkdir(parents=True)
    if not _make_psd(src_dir / "a.psd") or not _make_psd(src_dir / "sub" / "b.psd"):
        pytest.skip("could not create test PSDs")
    (src_dir / "notes.txt").write_text("not renderable")

    result = RenderPngHandler().run(ctx, src_dir)
    assert result.ok
    out_root = ctx.output_dir()
    rels = sorted(Path(o).relative_to(out_root).as_posix() for o in result.outputs)
    assert rels == ["a.png", "sub/b.png"]
    for o in result.outputs:
        assert Path(o).exists() and Path(o).stat().st_size > 0


@pytest.mark.skipif(
    shutil.which("dcraw_emu") is None
    or (shutil.which("magick") is None and shutil.which("convert") is None),
    reason="libraw (dcraw_emu) and/or ImageMagick not available",
)
def test_render_raw_real(make_ctx: CtxFactory, tmp_path: Path) -> None:
    """Real two-stage raw render. Skipped on the mac (no libraw); covered by the
    dockerized smoke. Needs a sample raw at NCPT_TEST_RAW (else skip)."""
    import os

    sample = os.environ.get("NCPT_TEST_RAW")
    if not sample or not Path(sample).exists():
        pytest.skip("set NCPT_TEST_RAW to a sample raw file to run this")
    ctx = make_ctx(Path(sample).name)
    src = ctx.work_dir / Path(sample).name
    src.write_bytes(Path(sample).read_bytes())

    result = RenderJpgHandler().run(ctx, src)
    out = Path(result.outputs[0])
    assert out.exists() and out.stat().st_size > 0


# --- F4: more render source types (M8) ----------------------------------------


def _make_png_bytes(width: int = 1, height: int = 1) -> bytes:
    """Build a minimal, valid PNG (signature..IEND+CRC) with zlib/struct."""
    import struct
    import zlib

    def chunk(typ: bytes, data: bytes) -> bytes:
        body = typ + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)  # RGB, 8-bit
    # One uncompressed scanline per row: filter byte 0 + width*3 bytes.
    raw = b"".join(b"\x00" + b"\xff\x00\x00" * width for _ in range(height))
    idat = zlib.compress(raw)
    return sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


def test_new_raster_and_vector_exts_registered() -> None:
    for ext in (*RASTER_EXTS, *VECTOR_PAGE_EXTS, "svg", "svgz", *AFFINITY_EXTS):
        assert ext in RENDERERS
    # A couple resolve-and-route checks.
    assert resolve_renderer("tiff") is resolve_renderer("tif")
    assert resolve_renderer("heic") is resolve_renderer("webp")
    assert resolve_renderer("pdf") is resolve_renderer("ai")
    assert resolve_renderer("svg") is resolve_renderer("svgz")


def test_raster_png_argv(make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _fake_render(monkeypatch)
    ctx = make_ctx("scan.tiff")
    src = ctx.work_dir / "scan.tiff"
    src.write_bytes(b"II*\x00")
    result = RenderPngHandler().run(ctx, src)
    assert result.ok
    argv = captured[0]
    assert argv[0] == "/usr/bin/magick"
    assert argv[1] == f"{src}[0]"
    assert "-density" not in argv  # raster: no density
    assert argv[-1].endswith("scan.png")


def test_raster_jpg_flatten_white(make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _fake_render(monkeypatch, magic_bytes=b"\xff\xd8\xff")
    ctx = make_ctx("pic.webp")
    src = ctx.work_dir / "pic.webp"
    src.write_bytes(b"RIFF")
    result = RenderJpgHandler().run(ctx, src)
    assert result.ok
    argv = captured[0]
    assert argv[1] == f"{src}[0]"
    assert "-background" in argv and "white" in argv and "-flatten" in argv
    assert argv[argv.index("-quality") + 1] == "90"
    assert argv[-1].endswith("pic.jpg")


def test_vector_density_present_for_pdf_ai_eps_ps(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    for ext in ("pdf", "ai", "eps", "ps"):
        captured = _fake_render(monkeypatch)
        ctx = make_ctx(f"doc.{ext}")
        src = ctx.work_dir / f"doc.{ext}"
        src.write_bytes(b"%PDF")
        RenderPngHandler().run(ctx, src)
        argv = captured[0]
        # -density 150 must come BEFORE the input.
        assert argv[1:3] == ["-density", "150"], ext
        assert argv[3] == f"{src}[0]", ext
        assert argv[-1].endswith("doc.png"), ext


def test_svg_png_uses_rsvg(make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        shutil,
        "which",
        lambda name: f"/usr/bin/{name}" if name in ("rsvg-convert", "magick", "convert") else None,
    )
    captured: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured.append(argv)
        # rsvg-convert -o <out> <src> writes the output named after -o.
        out = argv[argv.index("-o") + 1] if "-o" in argv else argv[-1]
        Path(out).write_bytes(b"\x89PNG")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    ctx = make_ctx("logo.svg")
    src = ctx.work_dir / "logo.svg"
    src.write_text("<svg/>")
    result = RenderPngHandler().run(ctx, src)
    assert result.ok
    argv = captured[0]
    assert argv[0] == "/usr/bin/rsvg-convert"
    assert argv[1] == "-o"  # -o <out> <src>
    assert Path(argv[2]).name == "logo.png"
    assert argv[3] == str(src)


def test_svg_jpg_pipes_rsvg_into_convert(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        shutil,
        "which",
        lambda name: f"/usr/bin/{name}" if name in ("rsvg-convert", "magick", "convert") else None,
    )
    captured: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        captured.append(argv)
        # stage2 (convert) writes the output; stage1 (rsvg) returns PNG on stdout.
        if Path(argv[0]).name == "rsvg-convert":
            return subprocess.CompletedProcess(argv, 0, b"\x89PNGfake", b"")
        Path(argv[-1]).write_bytes(b"\xff\xd8\xff")
        assert kwargs.get("input") == b"\x89PNGfake"  # piped through
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    ctx = make_ctx("logo.svg")
    src = ctx.work_dir / "logo.svg"
    src.write_text("<svg/>")
    result = RenderJpgHandler().run(ctx, src)
    assert result.ok
    assert len(captured) == 2
    assert Path(captured[0][0]).name == "rsvg-convert"
    assert captured[0] == ["/usr/bin/rsvg-convert", str(src)]
    stage2 = captured[1]
    assert Path(stage2[0]).name in ("magick", "convert")
    assert stage2[1] == "png:-"  # reads stdin
    assert "-flatten" in stage2 and "white" in stage2
    assert stage2[-1].endswith("logo.jpg")


def test_svg_missing_rsvg_raises(make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        shutil, "which", lambda name: "/usr/bin/convert" if name == "convert" else None
    )
    ctx = make_ctx("logo.svg")
    src = ctx.work_dir / "logo.svg"
    src.write_text("<svg/>")
    with pytest.raises(RenderError, match="rsvg-convert"):
        RenderPngHandler().run(ctx, src)


# --- Affinity embedded-PNG carver (real, no binary) ---------------------------


def test_carver_extracts_embedded_png() -> None:
    png = _make_png_bytes()
    blob = b"junkheader" + png + b"trailingjunk"
    assert _carve_largest_png(blob) == png


def test_carver_no_png_returns_none() -> None:
    assert _carve_largest_png(b"no png here at all") is None


def test_carver_picks_largest_of_two() -> None:
    small = _make_png_bytes(1, 1)
    big = _make_png_bytes(4, 4)
    assert len(big) > len(small)
    blob = b"a" + small + b"b" + big + b"c"
    assert _carve_largest_png(blob) == big


def test_affinity_png_target_writes_carved_blob(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    png = _make_png_bytes(2, 2)
    ctx = make_ctx("design.afphoto")
    src = ctx.work_dir / "design.afphoto"
    src.write_bytes(b"AFFINITY" + png + b"tail")
    result = RenderPngHandler().run(ctx, src)
    out = Path(result.outputs[0])
    assert out.name == "design.png"
    assert out.read_bytes() == png  # carved preview written verbatim


def test_affinity_jpg_target_converts_carved_blob(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_magick(monkeypatch)
    captured: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured.append(argv)
        Path(argv[-1]).write_bytes(b"\xff\xd8\xff")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    png = _make_png_bytes(2, 2)
    ctx = make_ctx("design.afphoto")
    src = ctx.work_dir / "design.afphoto"
    src.write_bytes(b"AFFINITY" + png + b"tail")
    result = RenderJpgHandler().run(ctx, src)
    assert result.ok
    argv = captured[0]
    assert Path(argv[1]).name == "design.preview.png"  # carved PNG handed to convert
    assert "-flatten" in argv and "white" in argv
    assert argv[-1].endswith("design.jpg")


def test_affinity_no_preview_raises(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = make_ctx("empty.afphoto")
    src = ctx.work_dir / "empty.afphoto"
    src.write_bytes(b"no embedded png whatsoever")
    with pytest.raises(RenderError, match="no embedded preview"):
        RenderPngHandler().run(ctx, src)


def test_dir_walk_mixed_f4_formats_skips_txt(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F1 composition: a.tiff + b.pdf + c.svg + notes.txt → three render, txt skipped."""
    monkeypatch.setattr(
        shutil,
        "which",
        lambda name: f"/usr/bin/{name}" if name in ("rsvg-convert", "magick", "convert") else None,
    )
    captured: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured.append(argv)
        # SVG PNG path writes via rsvg -o <out>; others write argv[-1].
        out = argv[argv.index("-o") + 1] if "-o" in argv else argv[-1]
        Path(out).write_bytes(b"\x89PNG")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    ctx = make_ctx("Album", is_dir=True)
    src_dir = ctx.work_dir / "src" / "Album"
    src_dir.mkdir(parents=True)
    (src_dir / "a.tiff").write_bytes(b"II*\x00")
    (src_dir / "b.pdf").write_bytes(b"%PDF")
    (src_dir / "c.svg").write_text("<svg/>")
    (src_dir / "notes.txt").write_text("hi")

    result = RenderPngHandler().run(ctx, src_dir)
    assert result.ok
    out_root = ctx.output_dir()
    rels = sorted(Path(o).relative_to(out_root).as_posix() for o in result.outputs)
    assert rels == ["a.png", "b.png", "c.png"]
    assert result.message == "rendered 3 of 3 files"

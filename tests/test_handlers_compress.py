"""Tests for compress handlers: zip (real), rar (gated/mocked), 7z (mocked)."""

from __future__ import annotations

import shutil
import subprocess
import zipfile
from collections.abc import Callable
from pathlib import Path

import pytest

from ncpowertools.errors import HandlerError
from ncpowertools.handlers import resolve
from ncpowertools.handlers.base import HandlerContext
from ncpowertools.handlers.compress import RarHandler, SevenZipHandler, ZipHandler

CtxFactory = Callable[..., HandlerContext]


# --- zip (real, stdlib) -------------------------------------------------------


def test_zip_single_file(make_ctx: CtxFactory) -> None:
    ctx = make_ctx("report.txt")
    src = ctx.work_dir / "report.txt"
    src.write_text("the content")

    result = ZipHandler().run(ctx, src)

    assert result.ok
    out = Path(result.outputs[0])
    assert out.name == "report.txt.zip"
    with zipfile.ZipFile(out) as zf:
        assert zf.namelist() == ["report.txt"]
        assert zf.read("report.txt") == b"the content"


def test_zip_folder(make_ctx: CtxFactory) -> None:
    ctx = make_ctx("mydir", is_dir=True)
    src = ctx.work_dir / "mydir"
    (src / "sub").mkdir(parents=True)
    (src / "a.txt").write_text("a")
    (src / "sub" / "b.txt").write_text("b")

    result = ZipHandler().run(ctx, src)

    out = Path(result.outputs[0])
    assert out.name == "mydir.zip"
    with zipfile.ZipFile(out) as zf:
        names = set(zf.namelist())
    assert names == {"mydir/a.txt", "mydir/sub/b.txt"}


def test_zip_is_deterministic(make_ctx: CtxFactory) -> None:
    def build() -> bytes:
        ctx = make_ctx("d", is_dir=True)
        src = ctx.work_dir / "d"
        src.mkdir()
        (src / "x.txt").write_text("hello")
        (src / "y.txt").write_text("world")
        out = Path(ZipHandler().run(ctx, src).outputs[0])
        return out.read_bytes()

    assert build() == build()


def test_zip_via_registry(make_ctx: CtxFactory) -> None:
    handler = resolve("zip")
    assert isinstance(handler, ZipHandler)
    assert handler.name == "zip"


# --- rar (gated) --------------------------------------------------------------


def test_rar_disabled_raises(make_ctx: CtxFactory) -> None:
    ctx = make_ctx("f.txt", enable_rar=False)
    src = ctx.work_dir / "f.txt"
    src.write_text("x")
    with pytest.raises(HandlerError, match="RAR creation disabled"):
        RarHandler().run(ctx, src)


def test_rar_enabled_but_no_binary_raises(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = make_ctx("f.txt", enable_rar=True)
    src = ctx.work_dir / "f.txt"
    src.write_text("x")
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(HandlerError, match="rar binary not available"):
        RarHandler().run(ctx, src)


def test_rar_argv_mocked(make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = make_ctx("f.txt", enable_rar=True)
    src = ctx.work_dir / "f.txt"
    src.write_text("x")
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/rar" if name == "rar" else None)

    captured: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured.append(argv)
        Path(argv[5]).write_bytes(b"Rar!fake")  # out path is argv index 5
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = RarHandler().run(ctx, src)
    assert result.ok
    argv = captured[0]
    assert argv[0] == "/usr/bin/rar"
    assert argv[1] == "a"
    assert "-r" in argv and "-ep1" in argv and "-o+" in argv
    assert argv[5].endswith("f.txt.rar")


# --- 7z compress (mocked) -----------------------------------------------------


def test_7z_compress_argv_mocked(make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = make_ctx("f.txt")
    src = ctx.work_dir / "f.txt"
    src.write_text("x")
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/7z" if name == "7z" else None)

    captured: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured.append(argv)
        out = next(a for a in argv if a.endswith(".7z"))
        Path(out).write_bytes(b"7z\xbc\xaf")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = SevenZipHandler().run(ctx, src)
    assert result.ok
    argv = captured[0]
    assert argv[1] == "a"
    assert any(a.endswith("f.txt.7z") for a in argv)


def test_7z_compress_no_binary(make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = make_ctx("f.txt")
    src = ctx.work_dir / "f.txt"
    src.write_text("x")
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(HandlerError, match="7z binary not available"):
        SevenZipHandler().run(ctx, src)


# --- real rar test (skipped without binary) -----------------------------------


@pytest.mark.skipif(shutil.which("rar") is None, reason="rar binary not available")
def test_rar_real(make_ctx: CtxFactory) -> None:
    ctx = make_ctx("f.txt", enable_rar=True)
    src = ctx.work_dir / "f.txt"
    src.write_text("real rar content")
    result = RarHandler().run(ctx, src)
    out = Path(result.outputs[0])
    assert out.exists() and out.suffix == ".rar"
    assert out.read_bytes().startswith(b"Rar!")

"""Tests for the extract handler: real stdlib formats + safety guards + mocked 7z/rar."""

from __future__ import annotations

import gzip
import io
import shutil
import subprocess
import tarfile
import zipfile
from collections.abc import Callable
from pathlib import Path

import pytest

from ncpowertools.errors import ArchiveTooLargeError, HandlerError, UnsafeArchiveError
from ncpowertools.handlers import resolve
from ncpowertools.handlers.archives import (
    ExtractHandler,
    _archive_kind,
    _parse_7z_listing,
    _parse_unrar_listing,
    _safe_join,
    _stem_for,
)
from ncpowertools.handlers.base import HandlerContext

CtxFactory = Callable[..., HandlerContext]


# --- fixture builders ---------------------------------------------------------


def _make_zip(path: Path, members: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in members.items():
            zf.writestr(name, data)


def _make_tar_gz(path: Path, members: dict[str, bytes]) -> None:
    with tarfile.open(path, "w:gz") as tf:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))


# --- detection / helpers ------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "kind"),
    [
        ("a.zip", "zip"),
        ("a.tar", "tar"),
        ("a.tar.gz", "tar"),
        ("a.tgz", "tar"),
        ("a.tar.bz2", "tar"),
        ("a.7z", "7z"),
        ("a.rar", "rar"),
        ("a.gz", "gz"),
        ("a.txt", None),
        ("noext", None),
    ],
)
def test_archive_kind(name: str, kind: str | None) -> None:
    assert _archive_kind(name) == kind


@pytest.mark.parametrize(
    ("name", "stem"),
    [("foo.zip", "foo"), ("foo.tar.gz", "foo"), ("foo.tgz", "foo"), ("data.gz", "data")],
)
def test_stem_for(name: str, stem: str) -> None:
    assert _stem_for(name) == stem


def test_safe_join_rejects_traversal(tmp_path: Path) -> None:
    dest = (tmp_path / "dest").resolve()
    dest.mkdir()
    with pytest.raises(UnsafeArchiveError):
        _safe_join(dest, "../../etc/passwd")
    with pytest.raises(UnsafeArchiveError):
        _safe_join(dest, "/abs/path")
    # A normal nested member is fine.
    assert _safe_join(dest, "sub/file.txt") == (dest / "sub/file.txt")


# --- zip ----------------------------------------------------------------------


def test_extract_zip_real(make_ctx: CtxFactory) -> None:
    ctx = make_ctx("sample.zip")
    arc = ctx.work_dir / "sample.zip"
    _make_zip(arc, {"a.txt": b"hello", "sub/b.txt": b"world"})

    result = ExtractHandler().run(ctx, arc)

    assert result.ok
    dest = ctx.output_dir() / "sample"
    assert (dest / "a.txt").read_bytes() == b"hello"
    assert (dest / "sub/b.txt").read_bytes() == b"world"
    # Original archive untouched.
    assert arc.exists()
    assert {Path(p).name for p in result.outputs} == {"a.txt", "b.txt"}


def test_extract_zip_via_registry(make_ctx: CtxFactory) -> None:
    handler = resolve("extract")
    ctx = make_ctx("x.zip")
    arc = ctx.work_dir / "x.zip"
    _make_zip(arc, {"f": b"data"})
    assert handler.can_handle(ctx.src)
    assert handler.run(ctx, arc).ok


# --- tar.gz -------------------------------------------------------------------


def test_extract_tar_gz_real(make_ctx: CtxFactory) -> None:
    ctx = make_ctx("bundle.tar.gz")
    arc = ctx.work_dir / "bundle.tar.gz"
    _make_tar_gz(arc, {"x.txt": b"abc", "d/y.txt": b"def"})

    result = ExtractHandler().run(ctx, arc)

    dest = ctx.output_dir() / "bundle"
    assert (dest / "x.txt").read_bytes() == b"abc"
    assert (dest / "d/y.txt").read_bytes() == b"def"
    assert result.ok


# --- bare gz ------------------------------------------------------------------


def test_extract_bare_gz_real(make_ctx: CtxFactory) -> None:
    ctx = make_ctx("data.gz")
    arc = ctx.work_dir / "data.gz"
    with gzip.open(arc, "wb") as f:
        f.write(b"plain content")

    result = ExtractHandler().run(ctx, arc)

    dest = ctx.output_dir() / "data"
    assert (dest / "data").read_bytes() == b"plain content"
    assert result.ok


def test_extract_bare_gz_size_cap(make_ctx: CtxFactory) -> None:
    ctx = make_ctx("big.gz", max_uncompressed_size=10)
    arc = ctx.work_dir / "big.gz"
    with gzip.open(arc, "wb") as f:
        f.write(b"x" * 1000)

    with pytest.raises(ArchiveTooLargeError):
        ExtractHandler().run(ctx, arc)
    # Partial output cleaned.
    assert not (ctx.output_dir() / "big").exists()


# --- zip-slip -----------------------------------------------------------------


def test_extract_zipslip_rejected_writes_nothing(make_ctx: CtxFactory, tmp_path: Path) -> None:
    ctx = make_ctx("evil.zip")
    arc = ctx.work_dir / "evil.zip"
    # A member that tries to escape via ../../
    _make_zip(arc, {"ok.txt": b"safe", "../../evil.txt": b"pwned"})

    sentinel = tmp_path / "evil.txt"  # would be the escape target's neighborhood
    with pytest.raises(UnsafeArchiveError):
        ExtractHandler().run(ctx, arc)

    # Nothing written outside dest, and partial dest cleaned.
    assert not sentinel.exists()
    assert not (ctx.output_dir() / "evil").exists()


def test_extract_tar_symlink_escape_rejected(make_ctx: CtxFactory) -> None:
    ctx = make_ctx("link.tar")
    arc = ctx.work_dir / "link.tar"
    with tarfile.open(arc, "w") as tf:
        info = tarfile.TarInfo("link")
        info.type = tarfile.SYMTYPE
        info.linkname = "../../../../etc/passwd"
        tf.addfile(info)

    with pytest.raises(UnsafeArchiveError):
        ExtractHandler().run(ctx, arc)
    assert not (ctx.output_dir() / "link").exists()


# --- zip-bomb -----------------------------------------------------------------


def test_extract_zip_too_many_files(make_ctx: CtxFactory) -> None:
    ctx = make_ctx("many.zip", max_files=3)
    arc = ctx.work_dir / "many.zip"
    _make_zip(arc, {f"f{i}.txt": b"x" for i in range(10)})

    with pytest.raises(ArchiveTooLargeError):
        ExtractHandler().run(ctx, arc)
    assert not (ctx.output_dir() / "many").exists()


def test_extract_zip_size_bomb_declared(make_ctx: CtxFactory) -> None:
    # A zip whose declared uncompressed sizes exceed the cap.
    ctx = make_ctx("bomb.zip", max_uncompressed_size=100)
    arc = ctx.work_dir / "bomb.zip"
    _make_zip(arc, {"big.bin": b"x" * 5000})

    with pytest.raises(ArchiveTooLargeError):
        ExtractHandler().run(ctx, arc)
    assert not (ctx.output_dir() / "bomb").exists()


def test_corrupt_zip_raises_handlererror(make_ctx: CtxFactory) -> None:
    ctx = make_ctx("bad.zip")
    arc = ctx.work_dir / "bad.zip"
    arc.write_bytes(b"not a zip at all")
    with pytest.raises(HandlerError):
        ExtractHandler().run(ctx, arc)


# --- listing parsers ----------------------------------------------------------


def test_parse_7z_listing() -> None:
    # Real `7z l -slt` output: an archive-header block (its Path is the archive
    # file itself, often an absolute path) then a `----------` separator, then
    # the per-member blocks. Only members after the separator must be parsed.
    out = (
        "Path = /tmp/archive.7z\nType = 7z\n\n"
        "----------\n"
        "Path = a.txt\nSize = 100\nFolder = -\n\n"
        "Path = sub\nSize = 0\nFolder = +\n\n"
        "Path = sub/b.txt\nSize = 200\nFolder = -\n"
    )
    total, count, names = _parse_7z_listing(out)
    # The archive-header Path is ignored (it lives before the separator).
    assert total == 300
    assert "a.txt" in names and "sub/b.txt" in names
    assert "sub" not in names  # folder skipped
    assert "/tmp/archive.7z" not in names  # archive itself is not a member
    assert count == 2  # the two real files only


def test_parse_unrar_listing() -> None:
    out = (
        "Name: a.txt\nType: File\nSize: 100\n\n"
        "Name: d\nType: Directory\nSize: 0\n\n"
        "Name: d/b.txt\nType: File\nSize: 250\n"
    )
    total, count, names = _parse_unrar_listing(out)
    assert total == 350
    assert count == 2
    assert names == ["a.txt", "d/b.txt"]


# --- 7z / rar via mocked subprocess ------------------------------------------


def test_extract_7z_mocked(make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = make_ctx("a.7z")
    arc = ctx.work_dir / "a.7z"
    arc.write_bytes(b"\x37\x7a\xbc\xaf")  # 7z magic-ish

    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/7z" if name == "7z" else None)

    calls: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if argv[1] == "l":
            stdout = (
                "Path = a.7z\nType = 7z\n\n----------\n"
                "Path = inside.txt\nSize = 5\nFolder = -\n"
            )
            return subprocess.CompletedProcess(argv, 0, stdout, "")
        # extraction: actually create the file so outputs are collected.
        dest = next(a[2:] for a in argv if a.startswith("-o"))
        Path(dest, "inside.txt").parent.mkdir(parents=True, exist_ok=True)
        Path(dest, "inside.txt").write_text("hello")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = ExtractHandler().run(ctx, arc)
    assert result.ok
    # listing then extraction
    assert calls[0][1] == "l"
    assert calls[1][1] == "x"
    assert any(a.startswith("-o") for a in calls[1])


def test_extract_7z_listing_over_limit_mocked(
    make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = make_ctx("a.7z", max_uncompressed_size=10)
    arc = ctx.work_dir / "a.7z"
    arc.write_bytes(b"\x37\x7a\xbc\xaf")
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/7z" if name == "7z" else None)

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert argv[1] == "l", "extraction must not be attempted past the limit"
        stdout = "Path = big.7z\nType = 7z\n\n----------\nPath = big.bin\nSize = 5000\nFolder = -\n"
        return subprocess.CompletedProcess(argv, 0, stdout, "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(ArchiveTooLargeError):
        ExtractHandler().run(ctx, arc)


def test_extract_7z_failure_raises(make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = make_ctx("a.7z")
    arc = ctx.work_dir / "a.7z"
    arc.write_bytes(b"x")
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/7z" if name == "7z" else None)

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 2, "", "7z: cannot open")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(HandlerError, match="7z listing failed"):
        ExtractHandler().run(ctx, arc)


def test_extract_rar_argv_mocked(make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = make_ctx("a.rar")
    arc = ctx.work_dir / "a.rar"
    arc.write_bytes(b"Rar!")
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/unrar" if name == "unrar" else None)

    calls: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if argv[1] == "lt":
            return subprocess.CompletedProcess(
                argv, 0, "Name: inside.txt\nType: File\nSize: 5\n", ""
            )
        dest = argv[-1].rstrip("/")
        Path(dest, "inside.txt").write_text("hello")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = ExtractHandler().run(ctx, arc)
    assert result.ok
    assert calls[0][1] == "lt"
    assert calls[1][1] == "x"
    assert "-o+" in calls[1]


def test_extract_7z_missing_binary(make_ctx: CtxFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = make_ctx("a.7z")
    arc = ctx.work_dir / "a.7z"
    arc.write_bytes(b"x")
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(HandlerError, match="7z binary not available"):
        ExtractHandler().run(ctx, arc)


# --- real binary tests (skipped on hosts without the CLI) ---------------------


@pytest.mark.skipif(
    shutil.which("7z") is None and shutil.which("7za") is None,
    reason="7z binary not available",
)
def test_extract_7z_real(make_ctx: CtxFactory) -> None:
    ctx = make_ctx("real.7z")
    src_dir = ctx.work_dir / "payload"
    src_dir.mkdir()
    (src_dir / "hi.txt").write_text("real 7z content")
    arc = ctx.work_dir / "real.7z"
    binary = shutil.which("7z") or shutil.which("7za")
    subprocess.run(
        [binary, "a", str(arc), str(src_dir / "hi.txt")], check=True, capture_output=True
    )

    result = ExtractHandler().run(ctx, arc)
    assert result.ok
    assert (ctx.output_dir() / "real" / "hi.txt").read_text() == "real 7z content"

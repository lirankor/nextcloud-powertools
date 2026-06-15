"""Command-line interface: ``run | poll-once | selftest | list-tags``.

stdlib ``argparse`` only (no click). ``run`` and ``poll-once`` are wired but
their orchestration is M3 — they print a notice and exit 0 so the entrypoint
is usable now. ``selftest`` and ``list-tags`` work for real against a
configured Nextcloud.

selftest deliberately separates the local tool-presence check (always runnable,
no network) from the NC-reachability check, per DEMO.md M4.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from collections.abc import Sequence

from .config import Settings, load_settings
from .errors import ConfigError, NcApiError
from .logging import get_logger, setup_logging

log = get_logger("cli")

# CLI tools the worker may shell out to (M2). selftest reports presence.
REQUIRED_TOOLS = ["unzip", "zip", "tar", "gzip"]
OPTIONAL_TOOLS = ["7z", "7za", "unrar", "rar", "magick", "convert"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ncpowertools",
        description="Tag-triggered Nextcloud file operations worker.",
    )
    sub = parser.add_subparsers(dest="command", metavar="{run,poll-once,selftest,list-tags}")

    sub.add_parser("run", help="Run the worker (poller + webhook server) [M3].")
    sub.add_parser("poll-once", help="Run a single polling sweep then exit [M3].")
    sub.add_parser(
        "selftest",
        help="Probe NC capabilities, list/ensure trigger tags, check CLI tools.",
    )
    sub.add_parser("list-tags", help="Print all system tags and their ids.")
    return parser


def _check_tools() -> tuple[bool, list[str]]:
    """Return (all_required_present, report_lines)."""
    lines: list[str] = ["Tools:"]
    all_required = True
    for tool in REQUIRED_TOOLS:
        present = shutil.which(tool) is not None
        all_required = all_required and present
        lines.append(f"  [{'ok' if present else 'MISSING'}] {tool} (required)")
    for tool in OPTIONAL_TOOLS:
        present = shutil.which(tool) is not None
        lines.append(f"  [{'ok' if present else '--'}] {tool} (optional)")
    return all_required, lines


def cmd_selftest(settings: Settings) -> int:
    """Two-phase selftest: local tools first, then NC reachability."""
    from .nextcloud import NextcloudClient

    ok = True
    report: list[str] = ["== ncpowertools selftest =="]

    # --- Phase 1: local CLI tools (no network) ---
    tools_ok, tool_lines = _check_tools()
    report.extend(tool_lines)
    if not tools_ok:
        ok = False
        report.append("  -> some REQUIRED tools are missing")

    # --- Phase 2: Nextcloud reachability (separate failure domain) ---
    report.append("Nextcloud:")
    client = NextcloudClient(settings)
    try:
        major, minor, micro = client.capabilities()
        report.append(f"  [ok] reachable; version {major}.{minor}.{micro}")
        report.append(
            f"  [ok] AutoMkcol={'yes' if major >= 32 else 'no (per-level MKCOL)'}, "
            f"webhooks={'yes' if major >= 32 else 'no (poll-only)'}"
        )

        existing = {t.name for t in client.list_tags()}
        report.append(f"  [ok] {len(existing)} system tags present")
        for tag in settings.TAG_ACTIONS:
            if tag in existing:
                report.append(f"  [ok] trigger tag '{tag}' exists")
            else:
                spec = client.ensure_tag(tag)
                report.append(f"  [ok] trigger tag '{tag}' created (id={spec.id})")
    except (NcApiError, OSError) as exc:
        ok = False
        report.append(f"  [FAIL] Nextcloud check failed: {exc}")
    finally:
        client.close()

    report.append(f"== {'PASS' if ok else 'FAIL'} ==")
    print("\n".join(report))
    return 0 if ok else 1


def cmd_list_tags(settings: Settings) -> int:
    from .nextcloud import NextcloudClient

    with NextcloudClient(settings) as client:
        tags = client.list_tags()
    for tag in sorted(tags, key=lambda t: (t.id or 0)):
        print(f"{tag.id}\t{tag.name}")
    return 0


def cmd_not_implemented(name: str, milestone: str) -> int:
    print(f"'{name}' is not yet implemented ({milestone}).")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return 0

    # list-tags / selftest / run / poll-once all need config.
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    setup_logging(settings.LOG_LEVEL)

    if args.command == "selftest":
        return cmd_selftest(settings)
    if args.command == "list-tags":
        return cmd_list_tags(settings)
    if args.command == "poll-once":
        return cmd_not_implemented("poll-once", "M3")
    if args.command == "run":
        return cmd_not_implemented("run", "M3")
    parser.print_help()  # pragma: no cover - argparse guards this
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

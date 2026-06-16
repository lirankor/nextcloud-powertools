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
import signal
import sys
import threading
from collections.abc import Sequence

from .config import Settings, load_settings
from .errors import ConfigError, NcApiError
from .logging import get_logger, setup_logging

log = get_logger("cli")

# CLI tools the worker may shell out to (M2). selftest reports presence.
REQUIRED_TOOLS = ["unzip", "zip", "tar", "gzip"]
OPTIONAL_TOOLS = ["7z", "7za", "unrar", "rar", "magick", "convert", "dcraw_emu", "rsvg-convert"]


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
    import httpx

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

        # Shred config + trash/version capability report (F5). When enabled, the
        # operator needs to see whether a permanent purge is actually possible.
        if settings.ENABLE_SHRED:
            report.append("Shred (DESTRUCTIVE, opt-in) — ENABLED:")
            report.append(f"  [!!] SHRED_DIR='{settings.SHRED_DIR}' (confined to this folder)")
            report.append(
                f"  [!!] tags: request='{settings.SHRED_TAG}', "
                f"confirm='{settings.SHRED_CONFIRM_TAG}'"
            )
            caps = client.files_capabilities()
            trash = "enabled" if caps["undelete"] else "DISABLED (DELETE is immediately permanent)"
            report.append(f"  [..] trash (undelete): {trash}")
            if caps["undelete"]:
                if caps["delete_from_trash"]:
                    report.append("  [ok] delete_from_trash: allowed -> permanent purge possible")
                else:
                    report.append(
                        "  [!!] delete_from_trash: DISABLED -> permanent purge NOT possible "
                        "(confirm will FAIL with a clear note)"
                    )
            report.append(
                f"  [..] versioning={caps['versioning']}, "
                f"version_deletion={caps['version_deletion']} "
                "(versions auto-purged with the trash delete)"
            )
        else:
            report.append("Shred: disabled (ENABLE_SHRED=false) — shred tags ignored")
    except (NcApiError, httpx.HTTPError, OSError) as exc:
        # httpx transport errors (ConnectError/TimeoutException/…) are HTTPError
        # subclasses, NOT OSError — catch them so an unreachable NC produces the
        # clean FAIL report (and tool-check output) instead of a traceback. This
        # keeps the healthcheck/smoke tolerant of NC being down.
        ok = False
        report.append(f"  [FAIL] Nextcloud check failed: {exc}")
    finally:
        client.close()

    # --- Phase 3: Immich reachability (F6, opt-in; separate failure domain) ---
    if settings.ENABLE_IMMICH:
        if not _immich_selftest(settings, report):
            ok = False
    else:
        report.append("Immich: disabled (ENABLE_IMMICH=false) — immich tags ignored")

    report.append(f"== {'PASS' if ok else 'FAIL'} ==")
    print("\n".join(report))
    return 0 if ok else 1


def _immich_selftest(settings: Settings, report: list[str]) -> bool:
    """Probe the Immich server (ping/version/API-key/media-types). Returns ok.

    Tolerant of Immich being unreachable — reports a FAIL line instead of
    crashing (mirrors the NC two-phase pattern).
    """
    import httpx

    from .immich import ImmichError, ImmichService

    report.append("Immich (F6, opt-in) — ENABLED:")
    report.append(f"  [..] IMMICH_URL='{settings.IMMICH_URL}', tag='{settings.IMMICH_TAG}'")
    with ImmichService(settings) as immich:
        try:
            pong = immich.ping()
            ver = immich.version()
            report.append(
                f"  [{'ok' if pong else '!!'}] reachable; version {ver}"
                + ("" if pong else " (ping did not return pong)")
            )
            albums = immich.list_albums()  # 200 here = the API key works
            report.append(f"  [ok] API key valid; {len(albums)} album(s) visible")
            mt = immich.media_types()
            report.append(
                f"  [ok] accepted media: {len(mt['image'])} image + "
                f"{len(mt['video'])} video types"
            )
            return True
        except (ImmichError, httpx.HTTPError, OSError) as exc:
            report.append(f"  [FAIL] Immich check failed: {exc}")
            return False


def cmd_list_tags(settings: Settings) -> int:
    from .nextcloud import NextcloudClient

    with NextcloudClient(settings) as client:
        tags = client.list_tags()
    for tag in sorted(tags, key=lambda t: (t.id or 0)):
        print(f"{tag.id}\t{tag.name}")
    return 0


def cmd_poll_once(settings: Settings) -> int:
    """Run a single polling sweep then exit (cron-friendly / smoke target)."""
    from .nextcloud import NextcloudClient
    from .pipeline import Pipeline
    from .poller import Poller

    with NextcloudClient(settings) as client:
        pipeline = Pipeline(client, settings)
        poller = Poller(client, pipeline, settings)
        try:
            poller.sweep()
        except NcApiError as exc:
            log.error("poll-once failed", extra={"error": str(exc)})
            print(f"poll-once failed: {exc}", file=sys.stderr)
            return 1
    return 0


def cmd_run(settings: Settings) -> int:
    """Run the worker: poller thread + webhook server, concurrently.

    Shutdown is graceful on SIGTERM/SIGINT. If neither the poller nor the
    webhook server is enabled, we error out with guidance rather than idling.
    """
    import uvicorn

    from .nextcloud import NextcloudClient
    from .pipeline import Pipeline
    from .poller import Poller
    from .webhook import create_app

    poll_enabled = settings.POLL_INTERVAL > 0
    hook_enabled = bool(settings.WEBHOOK_SECRET)
    if not poll_enabled and not hook_enabled:
        print(
            "Nothing to run: set POLL_INTERVAL>0 to enable polling and/or "
            "WEBHOOK_SECRET to enable the webhook server.",
            file=sys.stderr,
        )
        return 2

    client = NextcloudClient(settings)
    pipeline = Pipeline(client, settings)
    poller = Poller(client, pipeline, settings)
    poller_thread: threading.Thread | None = None
    server: uvicorn.Server | None = None

    if poll_enabled:
        poller_thread = threading.Thread(
            target=poller.run_forever, name="ncpt-poller", daemon=True
        )
        poller_thread.start()
        log.info("poller thread started", extra={"interval": settings.POLL_INTERVAL})

    stop = threading.Event()

    def _handle_signal(signum: int, _frame: object) -> None:
        log.info("shutdown signal received", extra={"signal": signum})
        poller.stop()
        if server is not None:
            server.should_exit = True
        stop.set()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    try:
        if hook_enabled:
            app = create_app(pipeline, settings)
            config = uvicorn.Config(
                app,
                host=settings.WEBHOOK_HOST,
                port=settings.WEBHOOK_PORT,
                log_config=None,
                workers=1,
            )
            server = uvicorn.Server(config)
            log.info(
                "webhook server starting",
                extra={
                    "host": settings.WEBHOOK_HOST,
                    "port": settings.WEBHOOK_PORT,
                    "path": settings.WEBHOOK_PATH,
                },
            )
            # uvicorn installs its own signal handlers and blocks until exit.
            server.run()
        else:
            # Poller-only: block until a signal sets the stop event.
            log.info("running poller-only (no webhook secret set)")
            while not stop.is_set():
                stop.wait(1.0)
    finally:
        poller.stop()
        if poller_thread is not None:
            poller_thread.join(timeout=5.0)
        client.close()
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
        return cmd_poll_once(settings)
    if args.command == "run":
        return cmd_run(settings)
    parser.print_help()  # pragma: no cover - argparse guards this
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

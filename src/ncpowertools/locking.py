"""Per-fileid in-process locking + an idempotency guard.

The worker is a single process with low concurrency (a poller thread plus the
webhook server dispatching into a thread pool). Two events for the *same* file
can therefore overlap — e.g. the poller sweeps a file at the same moment its
webhook arrives. We must run the handler for a given fileid **at most once at a
time**, and a second concurrent event must be a no-op (not block, not double
upload).

:func:`file_lock` is a context manager that yields ``True`` if it acquired the
lock for ``fileid`` and ``False`` if that fileid is already being processed. On
``False`` the caller should skip. The guard is a simple registry of
``threading.Lock`` objects keyed by fileid plus an ``in_progress`` set, all
protected by one registry mutex. It is purely in-process (no cross-container
coordination — there is only one worker, per the locked single-account design).
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager

# One mutex guarding the registry below.
_registry_lock = threading.Lock()
# fileids currently being processed by some thread.
_in_progress: set[int] = set()


@contextmanager
def file_lock(fileid: int) -> Iterator[bool]:
    """Acquire the processing lock for ``fileid``.

    Yields ``True`` if the caller now holds the lock (and must do the work),
    ``False`` if another thread is already processing this fileid (the caller
    should skip). Releases on exit only when it was actually acquired.
    """
    acquired = False
    with _registry_lock:
        if fileid not in _in_progress:
            _in_progress.add(fileid)
            acquired = True
    try:
        yield acquired
    finally:
        if acquired:
            with _registry_lock:
                _in_progress.discard(fileid)


def is_processing(fileid: int) -> bool:
    """Whether ``fileid`` is currently being processed (test/introspection)."""
    with _registry_lock:
        return fileid in _in_progress


def _reset() -> None:
    """Clear all locking state — for tests only."""
    with _registry_lock:
        _in_progress.clear()

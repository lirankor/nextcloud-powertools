# DEVELOPER_BRIEF.md — standing init for every developer agent

You are a developer agent building **nextcloud-powertools**: a small, isolated, Dockerized
Python worker that performs file operations on Nextcloud files when a user tags them, entirely
over Nextcloud's WebDAV/OCS API (no shared volumes, no Docker socket, no NC app-container
access). Image publishes multi-arch to ghcr.io.

## FIRST, read the specs (they are ground truth — do not re-research):
`specs/PLAN.md`, `specs/CONTEXT.md` (source-verified API facts + curl/XML examples),
`specs/ARCHITECTURE.md` (layout, models, contracts, config), `specs/DEMO.md` (acceptance).
Then study existing code in `src/ncpowertools/` and `tests/` and **reuse it — don't duplicate**.

## Working rules
- **Python 3.12.** Stack: `httpx`, `pydantic` / `pydantic-settings`, `FastAPI` + `uvicorn`,
  `lxml` (or stdlib xml), `pytest` + `respx`. External tools (`unzip`,`7z`,`unrar`,`zip`,`rar`,
  `tar`,`gzip`, `magick`/`convert`) via `subprocess` with `timeout` + captured stderr — never Wand.
- Pin deps in `pyproject.toml`. Verify any library/CLI API against what's actually installed,
  not memory. Verify NC API behavior against `specs/CONTEXT.md` (which was source-verified).
- Keep `ruff` and `mypy` clean. Structured (JSON) logging via `ncpowertools.logging`.
- **Security is the point of this project.** Enforce: zip-slip/path-traversal guards on every
  archive format; zip-bomb limits (`MAX_UNCOMPRESSED_SIZE` + `MAX_FILES`); never issue a WebDAV
  DELETE on user content (extract makes a NEW subfolder, leaves the original); constant-time
  (`hmac.compare_digest`) webhook-secret check; run as non-root; drop caps in compose.
- Determinism in tests: no network (use `respx`), no reliance on wall-clock/RNG for assertions;
  use fixtures under `tests/fixtures/`.
- Binary-backed handlers (7z/rar/unrar/ImageMagick) may not have their CLI present on the build
  host (macOS) — unit-test them with subprocess mocks AND add a `@pytest.mark.skipif(not
  shutil.which(...))` real test; the genuine cross-arch exercise is the Docker smoke + CI build.

## Definition of done (per milestone)
- Code compiles/imports; `ruff` + `mypy` clean; all existing tests still pass + new tests for new
  logic pass; the milestone's acceptance cases in `specs/DEMO.md` behave as specified.
- Smoke-test the real entrypoint where applicable (`python -m ncpowertools selftest` etc.).
- Leave no processes running (stop any uvicorn/server you start).
- `git add -A && git commit` with a clear message; update `specs/PLAN.md` (your milestone → ✅
  + one change-log line). Final message: what you built, how each acceptance case behaves,
  build/test/smoke results, deviations, and **gotchas for the next milestone**. Be honest about
  anything incomplete — do not paper over failures.

## How to run / verify
- Install: `pip install -e ".[dev]"` (or `uv pip install -e ".[dev]"`).
- Tests: `pytest -q`. Lint: `ruff check .`. Types: `mypy src`.
- Entrypoint: `python -m ncpowertools selftest` (needs env or a `.env`; for tests, mock).

## Your milestone
The supervisor injects your specific milestone (M#) below this brief in the dispatch prompt.
Build **only** that milestone's scope — do not build other milestones. If no milestone is given,
ask; do not start the whole project.

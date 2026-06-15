# DEMO.md — acceptance / pass criteria per milestone

Reproducible criteria. "Mocked NC" = `respx`-mocked httpx transport; no live instance here.

## M1 — Scaffold + config + Nextcloud client core
- `pip install -e ".[dev]"` succeeds; `python -m ncpowertools --help` lists `run|poll-once|
  selftest|list-tags`.
- `Settings` loads from env + `.env`; missing required vars (`NEXTCLOUD_URL`, `NC_USER`,
  `NC_APP_PASSWORD`) raise a clear error; `TAG_ACTIONS` parses both JSON and `a:b,c:d` forms.
- `NextcloudClient`, against mocked NC, correctly:
  - `capabilities()` parses `ocs.data.version` → `(major,minor,micro)`.
  - `download(path) -> bytes` GETs the right URL with Basic auth + percent-encoded segments.
  - `upload(path, data)` PUTs; `ensure_dir(path)` uses `X-NC-WebDAV-AutoMkcol` on ≥32 else
    per-level MKCOL.
  - `resolve_fileid(fileid) -> FileRef` sends the `oc:filter-files`/`oc:fileid` REPORT and
    parses href→path, resourcetype→is_dir.
  - `list_tags() -> list[TagSpec]` parses the systemtags PROPFIND.
  - `ensure_tag(name) -> TagSpec` creates on miss (parses `Content-Location` id), treats 409 as
    exists.
  - `search_by_tag(tagid) -> list[FileRef]` sends the `oc:systemtag` REPORT and parses results.
  - `assign_tag` / `remove_tag` hit the relations PUT/DELETE.
  - `notify(uid, short, long)` posts OCS `admin_notifications` (only when enabled).
- `ruff` + `mypy` clean; `pytest -q` green (client tests use `respx`).

## M2 — Action handlers + registry
- Registry resolves action names → handlers; `TAG_ACTIONS` map drives tag→action.
- **extract**: given `sample.zip`/`.tar.gz`/`.7z`(mock)/`.gz`, extracts into a subfolder named
  after the archive stem; returns the list of extracted relative paths. Leaves the archive.
  - `evil-zipslip.zip` (member `../../etc/x`) → raises `UnsafeArchiveError`, writes nothing
    outside dest. Symlink-escape member likewise rejected.
  - `bomb.zip` exceeding `MAX_FILES` or `MAX_UNCOMPRESSED_SIZE` → raises, partial output cleaned.
- **zip**: a file and a folder each compress to `<name>.zip`; round-trips (unzip lists expected
  entries).
- **rar**: with `ENABLE_RAR=false` → handler raises a clear "disabled" error; with the binary
  present + enabled, produces a `.rar` (real test skipped if `rar` absent).
- **render-png**: `sample.psd` → PNG; argv is `["magick"|"convert","<src>[0]","-background",
  "none","<out>.png"]` (assert argv via subprocess mock; real render skipped if no `magick`).
  PNG retains alpha (real test).
- **render**: `sample.psd` → JPG; argv includes `-background white -flatten`. Registry lets a
  new source ext be added in a few lines (test: register a dummy ext, assert it resolves).
- `ruff`/`mypy` clean; `pytest -q` green.

## M3 — Orchestration: trigger → pipeline
- `pipeline.process(event)` against mocked NC: resolve→download→run handler→upload to the
  **parent folder**→remove trigger tag→clean temp. Asserts: upload PUT targets the parent dir;
  no DELETE on the original; trigger-tag relation DELETE issued exactly once on success.
- Failure path: handler raises → trigger tag NOT removed; `ERROR_TAG` assigned (if set); temp
  cleaned; structured error logged; optional notify called when `NOTIFY=true`.
- Idempotency/lock: two concurrent events for the same fileid → only one runs the handler.
- **Webhook**: POST to `WEBHOOK_PATH` with correct secret header → 200 + event dispatched;
  wrong/missing secret → 401 (constant-time compare); `TagAssignedEvent` and `MapperEvent`
  (assign) payloads both parse; unassign ignored. `GET /healthz` → 200.
- **Poller**: `poll-once` runs a systemtag-search sweep per trigger tag and processes results
  (mocked).
- `python -m ncpowertools selftest` against mocked NC prints a green report and exits 0.
- No leftover processes. `ruff`/`mypy` clean; `pytest -q` green.

## M4 — Packaging & ops
- `docker build` (default args) succeeds; image runs as **non-root**; `magick`/`convert`,
  `unzip`,`7z`,`unrar`,`zip`,`tar`,`gzip` present; `rar` absent unless `--build-arg
  ENABLE_RAR=true`. policy.xml in place unlocking PSD/PDF/PS and applying limits.
- `docker run … selftest` against a mocked/un-reachable NC exits cleanly on tool checks (or a
  documented non-zero on NC unreachable — selftest separates tool-check from NC-check).
- A **real dockerized smoke**: in the built image, extract a real zip and render the bundled
  `sample.psd` → PNG/JPG (run inside the container, not the mac host) and assert outputs exist
  + PNG has alpha.
- `docker compose config` validates; compose sets `mem_limit`/`cpus` (or deploy limits),
  `cap_drop: [ALL]`, `read_only` where feasible, `restart: unless-stopped`, env via `.env`.
- `.github/workflows/docker-publish.yml` builds linux/amd64+arm64 and pushes to
  `ghcr.io/lirankor/nextcloud-powertools` on main/tags (verified by a successful CI run after push).
- `scripts/register-webhooks.sh` registers (and can list/delete) the tag webhooks via OCS with
  the secret header; README documents create-account→app-password→register→deploy, the tag
  reference, the security model, how to extend, and troubleshooting. `.env.example`, MIT
  `LICENSE` present.

## Final human checkpoint (⚠️ cannot be agent-verified)
Point the deployed worker at the real NC 33 instance with the service-account app-password, tag
a file in the web UI, and confirm: result appears in the same folder + trigger tag is removed.

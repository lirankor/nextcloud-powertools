# ARCHITECTURE.md

## Stack
- Python 3.12, `httpx` (sync client is fine; one worker, low concurrency), `pydantic` for
  config/payload models, `FastAPI` + `uvicorn` for the webhook server, `lxml` or stdlib
  `xml.etree` for WebDAV XML (prefer `lxml` for namespace ergonomics; stdlib acceptable).
- External tools via `subprocess` with `timeout` + captured stderr.
- Tests: `pytest`, `respx` (httpx mock transport) for client tests, real temp-file fixtures for
  archive handlers, subprocess-mock for binary-backed handlers.
- Lint/type: `ruff` + `mypy` (keep both clean). Build/dep: a single `pyproject.toml`.

## Repo layout
```
nextcloud-powertools/
├── pyproject.toml
├── README.md
├── LICENSE                      # MIT
├── .env.example
├── .gitignore
├── .dockerignore
├── Dockerfile
├── docker-compose.yml
├── policy.xml                   # ImageMagick coder unlock + limits (COPYed into image)
├── .github/workflows/docker-publish.yml
├── scripts/
│   └── register-webhooks.sh     # OCS webhook registration helper (+ list/delete)
├── src/ncpowertools/
│   ├── __init__.py
│   ├── __main__.py              # `python -m ncpowertools` entrypoint
│   ├── cli.py                   # arg parsing: run | poll-once | selftest | list-tags
│   ├── config.py                # Settings (pydantic-settings) from env / .env
│   ├── logging.py               # structured (JSON) logging setup
│   ├── models.py                # WebhookPayload, TagEvent, FileRef, ActionResult, TagSpec
│   ├── errors.py                # typed exceptions (NcApiError, UnsafeArchiveError, …)
│   ├── nextcloud/
│   │   ├── __init__.py
│   │   ├── client.py            # NextcloudClient: all WebDAV/OCS calls
│   │   └── webdav_xml.py        # build/parse REPORT & PROPFIND XML
│   ├── handlers/
│   │   ├── __init__.py          # registry: ACTIONS = {tag: handler}; resolve(tag)
│   │   ├── base.py              # Handler protocol; HandlerContext (paths, limits, logger)
│   │   ├── archives.py          # extract (zip/rar/7z/tar/tgz/gz) + safety guards
│   │   ├── compress.py          # zip / rar (opt-in) / 7z
│   │   └── render.py            # render registry: PSD→PNG/JPG; format→renderer map
│   ├── pipeline.py              # orchestration: trigger→download→action→upload→untag→clean
│   ├── locking.py               # per-fileid lock + idempotency guard
│   ├── webhook.py               # FastAPI app; constant-time secret check; enqueue
│   └── poller.py                # systemtag-search loop
└── tests/
    ├── conftest.py
    ├── fixtures/                # sample.zip, evil-zipslip.zip, bomb.zip, sample.psd, ...
    ├── test_config.py
    ├── test_client.py           # respx-mocked WebDAV/OCS
    ├── test_handlers_archives.py
    ├── test_handlers_compress.py
    ├── test_handlers_render.py  # subprocess-mocked + skip-if-no-magick real test
    ├── test_pipeline.py
    ├── test_webhook.py
    └── test_smoke.py            # selftest / end-to-end against mocked NC
```

## Data model (models.py)
- `TagSpec(id:int|None, name:str)` — a system tag.
- `FileRef(fileid:int, path:str, is_dir:bool, name:str, parent:str)` — resolved file.
- `TagEvent(uid:str, fileids:list[int], tagids:list[int], raw:dict)` — normalized from webhook
  payload OR synthesized by the poller.
- `ActionResult(ok:bool, outputs:list[str], message:str)` — what the handler produced
  (relative paths uploaded), for logging/notification.

## Handler contract (handlers/base.py)
```python
class Handler(Protocol):
    name: str                      # action name, e.g. "extract"
    def can_handle(self, src: FileRef) -> bool: ...    # e.g. extract: is it an archive?
    def run(self, ctx: HandlerContext, src_local: Path) -> ActionResult: ...
```
- `HandlerContext` carries: work dir, the resolved `FileRef`, limits (`MAX_UNCOMPRESSED_SIZE`,
  `MAX_FILES`), a logger, and a callback to write outputs (the pipeline supplies upload).
- Handlers operate on **local temp files only**; the pipeline owns download/upload. Handlers
  return the local output paths; the pipeline uploads them to the source's **parent folder**.
- **Registry** (`handlers/__init__.py`): `ACTIONS: dict[str, Handler]` keyed by action name.
  `TAG_ACTIONS` env maps tag-name→action-name (default map in config). Adding a tag = add a map
  entry; adding a render source type = register a renderer in `render.py`'s `RENDERERS` dict.

### render.py registry (extensibility requirement)
```python
RENDERERS: dict[str, Renderer] = {}     # keyed by lowercased source extension
def renderer(*exts): ...                 # decorator to register
# PSD shipped; adding SVG/TIFF/HEIC/AI = a few lines + ensure the delegate pkg is installed.
```
`render-png` → target PNG (`-background none`); `render` → target JPG
(`-background white -flatten`). A renderer maps (src_ext, target_fmt) → subprocess argv.

## Pipeline (pipeline.py) — the core flow
1. Receive `TagEvent` (from webhook or poller).
2. For each fileid: acquire per-fileid lock (skip if held). 
3. Resolve fileid→`FileRef` (REPORT). Determine which trigger tag(s) are present → action.
4. Idempotency: if an output already exists / a "done" marker, skip.
5. Download to `WORK_DIR/<fileid>/src` (GET; for folder + zip action, download-as-archive or
   walk — keep folder handling explicit).
6. Run handler → `ActionResult` with local outputs.
7. Upload each output via PUT into the **same parent folder** (MKCOL/AutoMkcol for the extract
   subfolder). Never DELETE user content.
8. On success: remove the trigger tag (DELETE relation); optional success notification.
9. On failure: log structured error; optionally assign an error tag (`powertools-error`,
   configurable) and/or notify; do NOT remove the trigger tag (so it's retriable) — but guard
   against infinite retry loops in the poller via the lock + a short failure backoff/marker.
10. Always clean `WORK_DIR/<fileid>`.

## Webhook server (webhook.py)
- FastAPI `POST /nc-hook` (path configurable). Read the shared secret from the configured header
  (`Authorization: Bearer …` or `X-Webhook-Secret`); `hmac.compare_digest` against
  `WEBHOOK_SECRET`; 401 on mismatch/missing. Parse envelope → `TagEvent` (handle both
  `TagAssignedEvent` `objectIds` and `MapperEvent` `objectId`+`eventType==assignTags`; ignore
  unassign). Dispatch to the pipeline (thread/executor) and return 200 fast. `GET /healthz`.
- Bind to `WEBHOOK_HOST`/`WEBHOOK_PORT` (default `0.0.0.0:8080`). TLS terminated by the reverse
  proxy; the README states the secret header is the trust boundary and TLS is required.

## Poller (poller.py)
- Every `POLL_INTERVAL` seconds (0 = disabled / webhook-only): for each configured trigger tag,
  systemtag-search REPORT → list of `FileRef`; synthesize a `TagEvent` per file (uid = NC_USER)
  and run the pipeline. Naturally idempotent via the lock + tag removal.

## Entrypoint (cli.py / __main__.py)
- `run` (default): start poller (if `POLL_INTERVAL>0`) and webhook server (if a secret is set);
  run both concurrently; graceful shutdown on SIGTERM/SIGINT.
- `poll-once`: single polling sweep then exit (good for cron-style or tests).
- `selftest`: probe capabilities/version, list system tags, verify each configured trigger tag
  exists (create if missing), verify required CLI tools are present (`which`), print a report,
  exit 0/!=0. This is the dockerized smoke target and the startup sanity check.
- `list-tags`: print system tags + ids.

## Config (config.py) — env vars (all in .env.example)
| Var | Default | Meaning |
|-----|---------|---------|
| `NEXTCLOUD_URL` | — (req) | Base URL, e.g. `https://cloud.example.com` |
| `NC_USER` | — (req) | Service account (admin) username |
| `NC_APP_PASSWORD` | — (req) | App password |
| `WEBHOOK_SECRET` | "" | Shared secret; if empty, webhook server disabled |
| `WEBHOOK_HEADER` | `Authorization` | Header carrying the secret (value `Bearer <secret>` if Authorization) |
| `WEBHOOK_PATH` | `/nc-hook` | Webhook route |
| `WEBHOOK_HOST`/`WEBHOOK_PORT` | `0.0.0.0`/`8080` | Bind |
| `TAG_ACTIONS` | see below | JSON or `tag:action,tag:action` map override |
| `ERROR_TAG` | `powertools-error` | Tag assigned on failure (empty = disabled) |
| `ENABLE_RAR` | `false` | Runtime guard; also a build arg for the binary |
| `POLL_INTERVAL` | `60` | Seconds; 0 = webhook-only |
| `MAX_UNCOMPRESSED_SIZE` | `2147483648` (2 GiB) | zip-bomb guard (bytes) |
| `MAX_FILES` | `10000` | zip-bomb guard (member count) |
| `WORK_DIR` | `/tmp/ncpowertools` | Temp scratch |
| `LOG_LEVEL` | `INFO` | |
| `NOTIFY` | `false` | Enable OCS notifications |
| `NC_ADMIN_USER`/`NC_ADMIN_PASSWORD` | = NC_USER/PW | Account used for notify/registration if different |
| `TARGET_USER` | = NC_USER | Namespace the worker operates in (polling + path resolution) |

Default `TAG_ACTIONS`: `extract:extract, zip:zip, rar:rar, render-png:render-png, render:render`.

## Non-goals
- No shared volumes, no Docker socket, no NC app-container access, no `occ` at runtime (setup
  only). No chunked upload (single PUT; document proxy limit). No multi-account fan-out (one
  service-account namespace, per locked decision). No native admin impersonation.

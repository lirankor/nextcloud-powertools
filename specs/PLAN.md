# PLAN.md — nextcloud-powertools

## Goal
A small, isolated, Dockerized **Python** worker that performs file operations on Nextcloud
files when a user tags them — done entirely over Nextcloud's **WebDAV/OCS API**. No shared
volumes, no Docker socket, no access to the Nextcloud app container. The image is published
multi-arch (amd64+arm64) to **ghcr.io** via GitHub Actions so it is pullable.

**Flow:** user adds a trigger tag to a file/folder → worker downloads via WebDAV → runs the
matching action → uploads result(s) via WebDAV into the **same parent folder** (native write,
so Nextcloud auto-indexes — no `occ` needed) → removes the trigger tag (marks done /
re-runnable) → cleans temp.

## Default tag → action map (configurable via `TAG_ACTIONS`)
| Tag         | Action                                                                 |
|-------------|------------------------------------------------------------------------|
| `extract`   | Decompress any archive (zip, rar, 7z, tar, tar.gz/tgz, gz) into a subfolder |
| `zip`       | Compress the tagged file/folder → `.zip`                               |
| `rar`       | Compress → `.rar` (opt-in build flag; default OFF; 7z is the open alt) |
| `render-png`| Render/convert → PNG (preserve transparency); starts with PSD          |
| `render`    | Render/convert → JPG; starts with PSD                                  |

`render-png`/`render` use a **format handler registry** — adding a source type (SVG, TIFF,
HEIC, AI, …) or a new tag is a few lines.

## Key research-derived facts (see CONTEXT.md for detail + sources)
- **Tag-assignment webhooks require NC32+** and fire from a **~5-min background cron**, not
  real-time. → Polling is the **primary/universal** path; webhooks are an NC32+ low-latency
  enhancement. Build both.
- **No HMAC/signature** in the official `webhook_listeners` app — outgoing auth is a **static
  header** only. → Worker validates a shared-secret header (constant-time) and requires TLS.
- A Nextcloud **admin cannot read other users' files over plain WebDAV** (no native
  impersonation). The worker acts in its **own namespace**; admin rights enable webhook
  registration + notifications. Folders to be processed must be **shared to the service
  account** (or in a Group Folder it belongs to). Optional NC32+ ephemeral-token mode is a
  documented extension.

## Decisions (locked)
- Repo: **`lirankor/nextcloud-powertools`**, public, on GitHub. Image: `ghcr.io/lirankor/nextcloud-powertools`.
- Auth: dedicated **admin** service account + scoped **app-password** (never the main password).
- Notifications: **included, optional, OFF by default** (OCS `admin_notifications`, needs admin).
- Language **Python 3.12**; HTTP via **httpx**; web server **FastAPI + uvicorn**; external
  tools via **subprocess** (not Wand). License **MIT**. Target **NC 33**, general **NC ≥ 30**
  (webhooks NC ≥ 32; polling works NC ≥ 30).

## Milestones
| # | Name | Status | Summary |
|---|------|--------|---------|
| M1 | Scaffold + config + Nextcloud client core | ✅ done | Package layout, env config, structured logging, `NextcloudClient` (capabilities/version, GET/PUT/MKCOL, fileid→path REPORT, systemtags list/create, assign/remove relation, systemtag-search REPORT, OCS notify). Mocked-httpx unit tests. |
| M2 | Action handlers + registry | ✅ done | Handler registry (tag→action), archive extract w/ zip-slip + zip-bomb guards, zip compress, rar compress (opt-in), render registry (PSD→PNG/JPG). Unit tests incl. malicious-archive fixtures; binary-backed handlers via subprocess + dockerized smoke. |
| M3 | Orchestration: trigger → pipeline | ✅ done | Pipeline tying client+handlers; per-file lock + idempotency; never-delete-original; error tag + optional notify. Webhook server (constant-time shared-secret validation, payload parse) + polling loop (systemtag search). Entrypoint, graceful shutdown. Unit + smoke. |
| M4 | Packaging & ops | ✅ done | Dockerfile (multi-stage slim non-root, tools, policy.xml, RAR build arg), docker-compose.yml (env, resource limits, cap_drop, restart), GH Actions multi-arch → ghcr, webhook setup script + docs, README, .env.example, LICENSE, .dockerignore/.gitignore. Local buildx smoke. |
| M5 | Directory-level render (F1) | ✅ done | `render`/`render-png` on a tagged folder → recursively render every registered-type file (PSD today) below it; outputs land beside each source, tree mirrored. See specs/FEATURE_REQUESTS.md F1. |
| M6 | Raw photo → JPG/PNG (F2) | ✅ done | Register camera-raw exts (CR2/CR3/NEF/ARW/DNG/RAF/ORF/RW2/PEF/SRW) in the render registry via two-stage `dcraw_emu`→TIFF→`convert`. Adds `libraw-bin`. Works on single files + folders (F1 walk). See FEATURE_REQUESTS.md F2. |

Status legend: ⬜ todo · 🟡 in progress · ✅ done · ⚠️ blocked/needs-human

## Execution model
Dark factory: one background developer agent per milestone, serial (each builds on the last).
Supervisor reviews each report, updates this plan, carries gotchas forward.

## Human checkpoints
- **Final live validation (⚠️ needs human):** point the worker at the real NC 33 instance with
  the service-account app-password; tag a file; confirm end-to-end. Agents cannot do this (no
  live creds / instance here). All other verification is mock-based unit tests + a dockerized
  smoke test + the CI build exercising real tools across both arches.

## Change log
- _(init)_ Specs written from 3-agent research fan-out (webhooks, WebDAV/OCS, ImageMagick+CI).
- **M1 ✅** — Scaffold + pyproject (py3.12, pinned deps), config (`Settings` + `TAG_ACTIONS` dual-form parser), JSON logging, models, typed errors, `webdav_xml` build/parse, `NextcloudClient` (all WebDAV/OCS methods per CONTEXT.md), argparse CLI (`run|poll-once|selftest|list-tags`; selftest splits tool-check from NC-check). 40 tests, ruff+mypy clean.
- **M2 ✅** — `handlers/base.py` (`Handler` Protocol + `HandlerContext` w/ `output_dir()`), registry `ACTIONS`/`resolve()`, `archives.py` extract (zip/tar/tgz/bz2/gz stdlib + 7z/unrar subprocess) with zip-slip + zip-bomb + symlink-escape guards and partial-cleanup, `compress.py` zip (deterministic) + rar (gated) + 7z, `render.py` `@renderer` registry shipping PSD→PNG/JPG. 92 tests (52 new) incl. malicious fixtures + subprocess-mock argv asserts; real 7z/rar/magick tests skip-if-absent. ruff+mypy clean.
- **M4 ✅** — Packaging & ops. Multi-stage `Dockerfile` (`python:3.12-slim-bookworm`; builder venv → slim runtime; runtime apt: imagemagick/ghostscript/librsvg2-bin/libheif1/p7zip-full/**unrar-free**/zip/unzip/tar/gzip/xz-utils, `--no-install-recommends` + cleaned lists; `policy.xml` → IM6+IM7 paths; non-root uid 10001; WORK_DIR `/tmp/ncpowertools`; EXPOSE 8080; HEALTHCHECK=selftest; CMD `run`; opt-in `ARG ENABLE_RAR` adds Debian non-free `rar`). `policy.xml` (PSD/PDF/PS/EPS/AI unlocked, MVG/MSL/URL disabled, low-power limits). `docker-compose.yml` (ghcr image + commented build, env_file, restart, `mem_limit`/`cpus`, `cap_drop:[ALL]`, `no-new-privileges`, `read_only`+tmpfs `/tmp`, ports gated to webhooks, healthcheck). `.github/workflows/docker-publish.yml` (test job: ruff+mypy+pytest on py3.12 → gated build job: buildx amd64+arm64, metadata-action tags, gha cache, push on main/tags only, build-only on PR). `scripts/register-webhooks.sh` (register/list/delete via OCS, `.env` autoload, version probe + NC<32 warn, double-backslash FQCN, header auth). `README.md`, `.env.example`, MIT `LICENSE`, `.dockerignore`. Re-enabled `readme` in pyproject. **Fixed** `cmd_selftest` to also catch `httpx.HTTPError` (was only `NcApiError`/`OSError`) so the healthcheck/smoke is tolerant of an unreachable NC instead of crashing. Verified locally: `docker build` OK (arm64), dockerized smoke — uid=10001, tools present (`convert`/`7z`/`7za`/`unrar`), `rar`+`magick` absent, real PSD→PNG(alpha=True)/JPG render + real zip extract (original preserved); `docker compose config` valid; ruff+mypy clean; 106 pass + 3 skip.
- **M3 ✅** — `pipeline.py` (resolve→tag-match→download→handler→upload-to-parent→untag→clean; never DELETEs user content; per-fileid lock; ERROR_TAG + optional notify on failure; in-process failure marker to stop poller hot-loops; folder compress via NC directory-GET-as-zip then local re-pack), `locking.py` (`file_lock(fileid)` context mgr yields False if already processing), `webhook.py` (FastAPI factory; constant-time secret check; `TagAssignedEvent` + `MapperEvent`(assign) parse; unassign/unknown→200 no-op; bg-executor dispatch; `/healthz`; lifespan shutdown), `poller.py` (`sweep()`/`run_forever()` per-tag systemtag-search → synth TagEvent), CLI `run` (poller thread + uvicorn server, SIGTERM/SIGINT graceful, errors if neither enabled) + `poll-once`. Added `client.download_dir_as_zip`. 20 new tests (pipeline/webhook/poll-once smoke). 109 pass + 3 skip, ruff+mypy clean.
- **M6 ✅ (F2)** — Camera RAW → JPG/PNG. **Generalized the renderer contract**: a `Renderer` is now a callable `render(src, out, fmt, scratch) -> None` that performs the full conversion via a shared `_run(argv)` helper (captures stderr, raises `RenderError`), rather than returning a single argv list — this lets multi-stage pipelines live behind the same contract. Refactored PSD to the new signature (one `_run` stage). New `_render_raw` (registered for `cr2 cr3 nef arw dng raf orf rw2 pef srw` via `RAW_EXTS`) runs two stages: `dcraw_emu -w -o 1 -q 3 -T -Z <scratch>/<stem>.decoded.tiff <src>` then `convert <tiff> -auto-orient -colorspace sRGB -depth 8 [-quality 90] <out>`; the temp TIFF lives under `work_dir/scratch` (explicit `-Z`, never beside the source) and is `unlink(missing_ok=True)`-cleaned in a `finally` (even on failure). `_render_one` now passes `ctx` so it can create the scratch dir; `_run_file`/`_run_dir` unchanged otherwise, so the F1 walk renders raws + PSDs identically. Raws bypass policy.xml (IM only opens the decoded TIFF). Dockerfile runtime apt: added `libraw-bin`; cli `OPTIONAL_TOOLS`: added `dcraw_emu`. README tag-reference + extend example updated to the new `(src, out, fmt, scratch)` signature. Tests: updated PSD/dummy/foobar renderers + the failure-message assertion to the new contract; added raw two-stage argv tests (jpg+png, exact dcraw_emu flags, `-Z` to scratch, convert flags, TIFF created-under-scratch-then-cleaned), no-decoder raise, TIFF-cleaned-on-stage2-failure, registry routing, all-raw-ext-registered, and a mixed `a.cr2`+`b.psd`+`notes.txt` dir-walk (F1 composition); real `dcraw_emu` test skips unless `NCPT_TEST_RAW` set. 128 pass + 5 skip, ruff+mypy clean. **Dockerized smoke** on a real Canon CR2 (rawpy sample, fetched on host + bind-mounted since the slim image has no curl): manual two-stage → `JPEG 1944x1296 8-bit sRGB`; the real `RenderJpgHandler`/`RenderPngHandler` code path produced valid `photo.jpg` (543859 B) + `photo.png` (3.08 MiB), scratch TIFF cleaned both times.
- **M5 ✅ (F1)** — Directory-level render. `render.py`: `RenderPngHandler`/`RenderJpgHandler.can_handle` now accept any directory (walk decides per file); single-file path refactored into `_render_one` + `_run_file`; new `_run_dir` walks recursively (sorted/deterministic), renders each registered ext to `output_dir()` at the **same relative path** (tree mirrored), enforces `MAX_FILES` as a **hard cap** (raise `HandlerError`), returns `ActionResult(ok, outputs, "rendered N of M files")`; **zero renderable → `ok=True, [], "nothing to render"`** (no error); per-file failure logged+skipped, but **all-fail → `RenderError`**. `pipeline.py`: render actions now flow folders through `download_dir_as_zip`→local unpack (reuses the compress path); `_upload_outputs` uploads each output preserving its rel path **into the tagged dir itself** for dir-render (vs the source's parent for files/extract/compress); originals never re-uploaded/deleted; trigger tag removed on success incl. the empty case. Docs: README tag-reference + extend section note folder support; `pipeline.py` module docstring corrected (render no longer skips dirs). Replaced the obsolete `test_render_on_folder_is_skipped` with dir-render success/empty/single-file pipeline tests; added handler tests (walk/tree-preserve, MAX_FILES cap, empty→nothing-to-render, per-file-continue, all-fail-raise, dummy-ext, real 2-level PSD skipif). 121 pass + 4 skip, ruff+mypy clean. Dockerized dir-render smoke with real IM6 `convert` passed.

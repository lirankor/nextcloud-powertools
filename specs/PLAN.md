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
| M1 | Scaffold + config + Nextcloud client core | 🟡 in progress | Package layout, env config, structured logging, `NextcloudClient` (capabilities/version, GET/PUT/MKCOL, fileid→path REPORT, systemtags list/create, assign/remove relation, systemtag-search REPORT, OCS notify). Mocked-httpx unit tests. |
| M2 | Action handlers + registry | ⬜ todo | Handler registry (tag→action), archive extract w/ zip-slip + zip-bomb guards, zip compress, rar compress (opt-in), render registry (PSD→PNG/JPG). Unit tests incl. malicious-archive fixtures; binary-backed handlers via subprocess + dockerized smoke. |
| M3 | Orchestration: trigger → pipeline | ⬜ todo | Pipeline tying client+handlers; per-file lock + idempotency; never-delete-original; error tag + optional notify. Webhook server (constant-time shared-secret validation, payload parse) + polling loop (systemtag search). Entrypoint, graceful shutdown. Unit + smoke. |
| M4 | Packaging & ops | ⬜ todo | Dockerfile (multi-stage slim non-root, tools, policy.xml, RAR build arg), docker-compose.yml (env, resource limits, cap_drop, restart), GH Actions multi-arch → ghcr, webhook setup script + docs, README, .env.example, LICENSE, .dockerignore/.gitignore. Local buildx smoke. |

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

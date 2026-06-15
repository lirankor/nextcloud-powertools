# nextcloud-powertools

[![docker-publish](https://github.com/lirankor/nextcloud-powertools/actions/workflows/docker-publish.yml/badge.svg)](https://github.com/lirankor/nextcloud-powertools/actions/workflows/docker-publish.yml)

A small, **isolated**, Dockerized Python worker that performs file operations on
your Nextcloud files **when you tag them** — entirely over Nextcloud's
**WebDAV/OCS API**. No shared volumes, no Docker socket, no access to the
Nextcloud app container. Multi-arch image published to
`ghcr.io/lirankor/nextcloud-powertools`.

## What it does

Add a trigger tag to a file (or folder) in the Nextcloud web UI. The worker
notices (via webhook or polling), downloads the file over WebDAV, runs the
matching action, **uploads the result(s) back into the same parent folder**
(Nextcloud auto-indexes the native write — no `occ` needed), then **removes the
trigger tag** to mark it done and make it re-runnable. Temp files are cleaned.

> **The original is never deleted.** `extract` writes into a new subfolder
> beside the archive; the archive stays. No action ever issues a WebDAV DELETE
> on your content. The only tag change is removing the trigger tag on success
> (and, optionally, adding an error tag on failure).

### Tag reference (default `TAG_ACTIONS`)

| Tag          | Action                                                                  |
|--------------|-------------------------------------------------------------------------|
| `extract`    | Decompress an archive (zip, rar, 7z, tar, tar.gz/tgz, tar.bz2/xz, gz) into a new subfolder |
| `zip`        | Compress the tagged file/folder → `<name>.zip`                          |
| `rar`        | Compress → `.rar` (opt-in build; default OFF — use `zip`/`7z` instead)  |
| `render-png` | Render/convert → PNG, preserving transparency (ships PSD)               |
| `render`     | Render/convert → JPG, flattened onto white (ships PSD)                   |

The map is configurable via `TAG_ACTIONS`. `render`/`render-png` use an
extensible renderer registry — adding a source type (SVG, TIFF, HEIC, AI, …) is
a few lines (see [How to extend](#how-to-extend)).

## Security model

Isolation is the whole point of this design:

- **No host coupling.** No shared volumes, no Docker socket, no Nextcloud
  app-container access. The worker talks only WebDAV/OCS over HTTP(S).
- **Runs unprivileged.** Non-root user (uid 10001), `cap_drop: [ALL]`,
  `no-new-privileges`, read-only root filesystem with a tmpfs for scratch.
- **Webhook auth = a shared-secret header, constant-time compared**
  (`hmac.compare_digest`). The official `webhook_listeners` app provides **no
  HMAC/signature** — the static header is the trust boundary, so the worker
  **must be behind a TLS-terminating reverse proxy**. Polling needs no inbound
  port at all.
- **Archive safety.** Every format (zip/tar/7z/rar/gz) is guarded against
  **zip-slip / path traversal** (members that escape the destination, absolute
  paths, symlink/hardlink escapes are rejected) and **zip-bombs**
  (`MAX_UNCOMPRESSED_SIZE` + `MAX_FILES`, enforced before and while extracting;
  partial output is cleaned on abort).
- **ImageMagick hardening** via a custom `policy.xml`: only the coders we render
  (PSD/PDF/PS/EPS/AI) are re-enabled, the classic RCE vectors (MVG/MSL/URL/HTTP
  delegates) stay disabled, and resource limits cap memory/disk/time.
- **Never deletes originals**; idempotent via a per-file lock + tag removal only
  after a verified upload.

## Setup

### 1. Create a dedicated Nextcloud service account (admin)

Create a new user (e.g. `powertools`) and make it an **admin**. Admin rights are
needed for **webhook registration** and (optional) **notifications**.

> **File-access caveat:** a Nextcloud admin **cannot read other users' files
> over plain WebDAV** — there is no native impersonation. The worker operates in
> its **own namespace** (`/remote.php/dav/files/powertools/`). For files owned
> by other users to be processed, **share those folders with the service
> account** (or put them in a **Group Folder** the account belongs to). Files
> the worker should touch must be reachable in its own file tree.

### 2. Generate an app-password

In the service account's **Settings → Security → Devices & sessions**, create an
app-password. Use that as `NC_APP_PASSWORD` — never the login password (it also
survives 2FA/OIDC enforcement).

### 3. Create the trigger tags

Create the system tags (`extract`, `zip`, `render-png`, `render`, …) as
**user-visible + user-assignable** in Nextcloud's admin **Administration
settings → Basic settings → Collaborative tags**, or just run `selftest` once —
it auto-creates any missing configured trigger tags.

### 4. Deploy via Docker Compose

```bash
cp .env.example .env        # then edit .env (NEXTCLOUD_URL, NC_USER, NC_APP_PASSWORD, …)
docker compose pull         # pulls ghcr.io/lirankor/nextcloud-powertools:latest
docker compose up -d
docker compose logs -f
```

Quickstart without compose:

```bash
docker pull ghcr.io/lirankor/nextcloud-powertools:latest
docker run -d --name ncpt --env-file .env \
  --cap-drop ALL --security-opt no-new-privileges:true \
  --read-only --tmpfs /tmp:size=2g \
  -p 8080:8080 \
  ghcr.io/lirankor/nextcloud-powertools:latest

# Sanity check (tools + NC reachability):
docker exec ncpt python -m ncpowertools selftest
```

You need **at least one** trigger mode enabled: `POLL_INTERVAL>0` (polling) and/or
`WEBHOOK_SECRET` (webhook server). Polling alone is the simplest, universal setup.

### 5. Register webhooks (NC ≥ 32) and/or enable polling

**Polling (universal, NC ≥ 30):** set `POLL_INTERVAL=60` in `.env`. No inbound
port needed. Done.

**Webhooks (low-latency, NC ≥ 32):** set `WEBHOOK_SECRET`, expose the worker
behind TLS, then register the listener:

```bash
NEXTCLOUD_URL=https://cloud.example.com \
NC_ADMIN_USER=powertools NC_ADMIN_PASSWORD='app-pw' \
WEBHOOK_URL=https://worker.example.com/nc-hook \
WEBHOOK_SECRET='same-secret-as-in-.env' \
  ./scripts/register-webhooks.sh register

./scripts/register-webhooks.sh --list           # confirm
./scripts/register-webhooks.sh --delete <id>     # remove later
```

The script reads `.env` automatically if present, probes the NC version, and
**warns if NC < 32** (tag webhooks won't fire — rely on polling). On the server
side, `occ webhook_listeners:list --output=json_pretty` shows the same list
(create/delete are OCS-only, which the script uses).

> Even with webhooks, Nextcloud dispatches them from a **~5-minute background
> cron**, not in real time. Keep polling on as the reliable path; treat webhooks
> as a latency improvement.

### Reverse proxy / TLS / body size

- The worker's webhook auth is a **shared header** — it **must** sit behind a
  TLS-terminating reverse proxy. Polling-only deployments need no inbound port.
- Raise the proxy's **request body-size limit** in front of *Nextcloud* (nginx
  defaults to 1 MB → `413` on larger uploads/PUTs). The worker uses single PUTs
  (no chunked upload).

## Configuration

All via environment (see `.env.example`). Mirrors `config.py`:

| Var | Default | Meaning |
|-----|---------|---------|
| `NEXTCLOUD_URL` | — (required) | Base URL, e.g. `https://cloud.example.com` |
| `NC_USER` | — (required) | Service account (admin) username |
| `NC_APP_PASSWORD` | — (required) | App-password |
| `WEBHOOK_SECRET` | `""` | Shared secret; empty ⇒ webhook server disabled |
| `WEBHOOK_HEADER` | `Authorization` | Header carrying the secret (`Bearer <secret>` if `Authorization`) |
| `WEBHOOK_PATH` | `/nc-hook` | Webhook route |
| `WEBHOOK_HOST` | `0.0.0.0` | Bind host (container-internal) |
| `WEBHOOK_PORT` | `8080` | Bind port (also `EXPOSE`d) |
| `TAG_ACTIONS` | see below | JSON map override (env form must be **JSON**) |
| `ERROR_TAG` | `powertools-error` | Tag assigned on failure (empty ⇒ disabled) |
| `ENABLE_RAR` | `false` | Enable the `rar` action (also a build arg for the binary) |
| `POLL_INTERVAL` | `60` | Polling seconds; `0` ⇒ webhook-only |
| `MAX_UNCOMPRESSED_SIZE` | `2147483648` | Zip-bomb guard (bytes) |
| `MAX_FILES` | `10000` | Zip-bomb guard (member count) |
| `WORK_DIR` | `/tmp/ncpowertools` | Temp scratch (under `/tmp` for read-only FS + tmpfs) |
| `LOG_LEVEL` | `INFO` | Log level |
| `NOTIFY` | `false` | Enable OCS notifications (needs notifications app + admin) |
| `NC_ADMIN_USER` / `NC_ADMIN_PASSWORD` | = `NC_USER` / `NC_APP_PASSWORD` | Account for notify/registration if different |
| `TARGET_USER` | = `NC_USER` | Namespace the worker operates in |

Default `TAG_ACTIONS` (set via env as **JSON**, not the compact form):

```json
{"extract":"extract","zip":"zip","rar":"rar","render-png":"render-png","render":"render"}
```

## How to extend

**Add a new tag → action:** map the tag name to an existing action in
`TAG_ACTIONS`, e.g. `{"backup":"zip"}` makes the `backup` tag zip things.

**Add a render source type:** register a renderer in
`src/ncpowertools/handlers/render.py` with the `@renderer` decorator. A renderer
maps `(src, out, target_fmt)` → an ImageMagick argv list. Example for SVG (the
`librsvg2-bin` delegate is already in the image; TIFF/HEIC similar):

```python
@renderer("svg")
def _render_svg(src: Path, out: Path, fmt: str) -> list[str]:
    binary = magick_binary()              # "magick" or IM6 "convert"
    if fmt == "png":
        return [binary, "-background", "none", str(src), str(out)]
    return [binary, "-background", "white", "-flatten", str(src), str(out)]
```

Then both `render-png` (PNG) and `render` (JPG) work for that extension. For a
new delegate (e.g. HEIC), add the apt package (`libheif1`) in the `Dockerfile`
runtime stage — most are already installed.

## RAR opt-in

Creating `.rar` files needs the **proprietary, non-free** `rar` binary, so it is
**OFF by default** and not in the published image. The default image extracts
RARs (via `unrar-free`) but cannot create them. Open alternatives: the `zip` and
`7z` actions (`p7zip-full` is bundled).

To build an image that can create `.rar`:

```bash
docker build --build-arg ENABLE_RAR=true -t ncpowertools-rar .
```

…and set `ENABLE_RAR=true` in `.env`. The runtime guard refuses the `rar` action
unless both the build arg baked the binary **and** the env flag is true.

## Troubleshooting

- **Webhook never fires** → You're on NC < 32 (tag webhooks unsupported), or the
  ~5-min cron hasn't run. Use polling (`POLL_INTERVAL>0`).
- **`413 Request Entity Too Large`** → Reverse-proxy body-size limit (nginx
  default 1 MB). Raise it in front of Nextcloud.
- **`403` when assigning a tag / nothing happens** → The tag isn't
  user-assignable, or the service account lacks write access to the file. Make
  the tag user-visible+assignable; share the folder to the account.
- **PSD/PDF render fails** ("attempt to perform an operation not allowed by the
  security policy") → ImageMagick `policy.xml` not in place / wrong path. The
  image installs it at `/etc/ImageMagick-6/policy.xml` (IM6 on bookworm).
- **"file not found" / nothing to process** → The file isn't in the service
  account's own namespace. Share the folder to the account or use a Group Folder.

## Nextcloud version matrix

| NC version | Polling | Webhooks |
|------------|---------|----------|
| ≥ 30, < 32 | ✅       | ❌ (tag webhooks not serializable) |
| ≥ 32       | ✅       | ✅ (`OCP\SystemTag\TagAssignedEvent`) |

Targeted/tested against **NC 33**; general support **NC ≥ 30** (webhooks NC ≥ 32).

## Development

```bash
pip install -e ".[dev]"
ruff check . && mypy src && pytest -q
python -m ncpowertools selftest    # needs env / a .env
```

## License

MIT © 2026 Liran Koren — see [LICENSE](LICENSE).

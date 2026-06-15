# CONTEXT.md — domain ground truth (source-verified)

All facts below were verified from Nextcloud server source (branches stable30–stable33),
official docs, and the Docker/ImageMagick docs during the research phase. Treat this as
authoritative; verify any API against the **installed** NC version at runtime, not memory.

Conventions in examples: base `https://cloud.example.com`, service user `powertools`, app
password as the Basic-auth password.

---

## 1. Auth model
- HTTP **Basic auth**: `(NC_USER, NC_APP_PASSWORD)`. App passwords are created in personal
  Security settings and are required when 2FA/OIDC is enforced. Never the main password.
- The service account is an **admin** (our locked decision) — admin rights are needed for
  **webhook registration** (OCS) and **notifications** (OCS `admin_notifications`).
- **CRITICAL caveat:** a Nextcloud admin **cannot read other users' files over plain WebDAV**
  (`/remote.php/dav/files/<otheruser>/` → 403; there is no native impersonation). The worker
  therefore operates in **its own namespace** (`/remote.php/dav/files/<NC_USER>/`). For other
  users' files to be processed, those folders must be **shared with the service account** or
  live in a **Group Folder** it belongs to. Document this clearly.
- Optional NC32+ enhancement (documented extension, not the baseline): register the webhook
  with `tokenNeeded` so NC delivers short-lived (~1h) per-user tokens in the payload; the
  worker can then act AS the triggering user. Webhook-path only; polling can't use it.

## 2. WebDAV (namespace base: `/remote.php/dav/files/<NC_USER>/`)
Namespaces used everywhere: `DAV:`→`d`, `http://owncloud.org/ns`→`oc`,
`http://nextcloud.org/ns`→`nc`.

- **Download (GET):** `GET /remote.php/dav/files/<user>/<path>`. Percent-encode each path
  *segment* (`urllib.parse.quote(path, safe="/")`); keep `/` as separator. A GET on a
  directory with `Accept: application/zip` (or `application/x-tar`) downloads it as an archive
  (Nextcloud extension) — useful for the `zip` action on folders, but we will build archives
  locally for full control.
- **Upload (PUT):** `PUT /remote.php/dav/files/<user>/<path>` with raw bytes. Overwrites if
  present. Optional headers: `X-OC-MTime`, `OC-Checksum` (`SHA256:<hex>` etc.).
- **MKCOL:** `MKCOL /remote.php/dav/files/<user>/<path>` — one level at a time (parents must
  exist). **NC32+ shortcut:** header `X-NC-WebDAV-AutoMkcol: 1` on a PUT auto-creates missing
  parents (NOT on NC30/31 — detect version and fall back to per-level MKCOL).
- **Upload size:** NC has no fixed WebDAV cap; the practical limit is PHP
  (`upload_max_filesize`/`post_max_size`) + reverse-proxy body limit (nginx default 1 MB if
  unset!). Single PUT is fine for typical files; chunked upload (v2) is for >few-hundred-MB.
  We use single PUT and document the proxy-limit gotcha.

### fileid → path (SEARCH)
The webhook payload gives `objectIds` (= fileid) and `tagIds`, **no path, no tag name**.

> **CORRECTION (M7, verified live on NC 33.0.5).** The original research here was WRONG.
> Resolving fileid→path with an `oc:filter-files` REPORT carrying an `<oc:fileid>`
> **filter-rule** does **NOT** work on Nextcloud: NC **silently ignores** the `oc:fileid`
> filter-rule and returns an empty multistatus, so every fileid resolved to "not found" and
> nothing was processed. (The `oc:fileid` filter-rule is honoured only *inside* an
> `<oc:systemtag>` search — that's why the polling path via `search_by_tag` works.) Also note:
> ownCloud's `/remote.php/dav/meta/{fileid}` endpoint (`oc:meta-path-for-user`) does **NOT exist
> in Nextcloud** — there is no `Meta` collection in NC's DAV `RootCollection`, so that approach
> would fail the same way. **Do not reintroduce either.**

The supported, documented Nextcloud resolver is the WebDAV **SEARCH** method:
```
SEARCH /remote.php/dav/   (Content-Type: application/xml)
```
```xml
<?xml version="1.0" encoding="UTF-8"?>
<d:searchrequest xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">
  <d:basicsearch>
    <d:select><d:prop>
      <oc:fileid/><d:getcontenttype/><d:getlastmodified/><d:resourcetype/>
    </d:prop></d:select>
    <d:from><d:scope>
      <d:href>/files/<user></d:href><d:depth>infinity</d:depth>
    </d:scope></d:from>
    <d:where><d:eq>
      <d:prop><oc:fileid/></d:prop><d:literal>12345</d:literal>
    </d:eq></d:where>
    <d:orderby/>
  </d:basicsearch>
</d:searchrequest>
```
`oc:fileid` is both *selectable* and *searchable* per the NC WebDAV Search docs, and
`d:resourcetype` is selectable — so one SEARCH returns the path **and** is_dir. Each
`<d:response>`'s `<d:href>` is the path; URL-decode and strip the
`/remote.php/dav/files/<user>/` prefix. `<d:resourcetype>` containing `<d:collection/>` ⇒ folder.
The scope is the worker's own `/files/<user>` namespace (admins can't read other users' files
anyway, §1). **Better still:** the poller already has the full `FileRef` (with path) from the
`oc:systemtag` search, so it carries it through the event and skips fileid resolution entirely;
SEARCH is only used on the webhook path. Source: NC Developer Manual → Client APIs → WebDAV →
Search.

## 3. System tags
- **List all tags + ids — PROPFIND** `/remote.php/dav/systemtags/` (`Depth: 1`), request props
  `<oc:id/>`, `<oc:display-name/>`, `<oc:user-visible/>`, `<oc:user-assignable/>`,
  `<oc:can-assign/>`. Each `<d:response>` href is `/remote.php/dav/systemtags/{id}`; `<oc:id>`
  is the numeric tag id.
- **Tags on one file — PROPFIND** `/remote.php/dav/systemtags-relations/files/<fileid>`
  (`Depth: 1`) → one `<d:response>` per assigned tag.
- **All files carrying a tag (POLLING fallback) — REPORT** on the user root with an
  `<oc:systemtag>` rule (numeric tag id). Multiple `<oc:systemtag>` rules are AND-combined.
  This is exactly how the web UI's tag view queries — reliable across NC30–33:
```xml
<oc:filter-files xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns" xmlns:nc="http://nextcloud.org/ns">
  <d:prop><oc:fileid/><d:getlastmodified/><d:getcontenttype/><d:resourcetype/></d:prop>
  <oc:filter-rules><oc:systemtag>7</oc:systemtag></oc:filter-rules>
</oc:filter-files>
```
- **Assign tag — PUT** `/remote.php/dav/systemtags-relations/files/<fileid>/<tagid>` (empty
  body) → `201`. Tag must be user-visible + user-assignable, and the user needs write access.
- **Remove tag — DELETE** `/remote.php/dav/systemtags-relations/files/<fileid>/<tagid>` → `204`.
- **Create tag if missing — POST** `/remote.php/dav/systemtags/` JSON
  `{"name":"…","userVisible":true,"userAssignable":true}` → `201`; the new id is in the
  **`Content-Location`** response header (parse trailing id). `409 Conflict` = already exists →
  re-list to get id. Idempotent pattern: list first; create on miss; treat 409 as exists.

## 4. OCS APIs (header `OCS-APIRequest: true` mandatory; add `Accept: application/json`)
- **Capabilities / version probe:** `GET /ocs/v2.php/cloud/capabilities?format=json`. Version
  at `ocs.data.version` (`major`,`minor`,`micro`,`string`). No admin needed. Use at startup to
  branch behavior (AutoMkcol & webhooks only on major ≥ 32).
- **Notify a user (admin):** `POST /ocs/v2.php/apps/notifications/api/v2/admin_notifications/<userId>`
  params `shortMessage` (≤255, required), `longMessage` (≤4000). Works NC30–33 (v2 deprecated
  since 30 but functional). v3 form uses `subject`/`message`. Needs the notifications app +
  admin. Optional feature, OFF by default.

## 5. Webhooks — `webhook_listeners` app
- **occ:** only `occ webhook_listeners:list [--output=json_pretty]` exists. **Create/delete are
  OCS-only.**
- **OCS endpoints** (admin Basic auth + `OCS-APIRequest: true`, JSON):
  - List: `GET  /ocs/v2.php/apps/webhook_listeners/api/v1/webhooks`
  - Create: `POST /ocs/v2.php/apps/webhook_listeners/api/v1/webhooks`
  - Delete: `DELETE /ocs/v2.php/apps/webhook_listeners/api/v1/webhooks/{id}`
- **Create body fields:** `httpMethod` (e.g. `POST`), `uri` (worker callback URL), `event`
  (PHP event FQCN), optional `eventFilter` (Mongo-style), `userIdFilter`, `headers`,
  `authMethod` (`none`|`header`), `authData` (header map for `header`), `tokenNeeded`.
- **Tag-assignment event (NC32+):** use `event = "OCP\\SystemTag\\TagAssignedEvent"`
  (assignment-only; sibling `TagUnassignedEvent`). Legacy `OCP\\SystemTag\\MapperEvent` covers
  assign+unassign (branch on `eventType`). **These emit a webhook-serializable payload only
  from NC32+** — on NC30/31 the webhook silently never fires for tag events.
- **Payload envelope** NC POSTs:
```json
{ "event": { "class": "OCP\\SystemTag\\TagAssignedEvent",
             "objectType": "files", "objectIds": ["75"], "tagIds": [1] },
  "user": { "uid": "alice", "displayName": "Alice" },
  "time": 1700100000,
  "authentication": { /* present only if tokenNeeded set */ } }
```
  (`MapperEvent` uses singular `objectId` + an `eventType` discriminator.)
- **Outgoing auth = static header ONLY. No HMAC, no signature.** (The SHA256-signature claim
  online belongs to the third-party `kffl/nextcloud-webhooks` app — different project.) So:
  register with `authMethod:"header"`, `authData:{"Authorization":"Bearer <WEBHOOK_SECRET>"}`
  (or `X-Webhook-Secret`); the worker compares it **constant-time** (`hmac.compare_digest`) and
  is served over **TLS only**. The shared header is the trust boundary.
- **Latency:** webhooks dispatch from a background job (~5-min cron) — not real-time. Hence
  polling is primary.
- **Example registration curl:** see ARCHITECTURE / the setup script milestone.

## 6. Example registration (note double-backslash in JSON FQCN)
```bash
curl -u powertools:APP_PW -H 'OCS-APIRequest: true' -H 'Content-Type: application/json' \
  -H 'Accept: application/json' -X POST \
  'https://cloud.example.com/ocs/v2.php/apps/webhook_listeners/api/v1/webhooks' \
  -d '{"httpMethod":"POST","uri":"https://worker.example.com/nc-hook",
       "event":"OCP\\SystemTag\\TagAssignedEvent",
       "authMethod":"header","authData":{"Authorization":"Bearer <WEBHOOK_SECRET>"}}'
```

## 7. ImageMagick (rendering) — Debian-slim, IM6 default
- **PSD → PNG (preserve alpha, merged composite only):** `magick "in.psd[0]" -background none out.png`
  (IM6: `convert`). The `[0]` selects the flattened composite Photoshop embeds; without an
  index IM loads all layers (wrong/multi-file output). Fallback if no composite: `"in.psd" -flatten`.
- **PSD → JPG (flatten onto white):** `magick "in.psd[0]" -background white -flatten -quality 90 out.jpg`.
- **Packages:** `imagemagick` (PSD native, no extra). For the extensible registry: SVG →
  `librsvg2-bin`; AI/PDF/PS/EPS → `ghostscript`; HEIC → `libheif1`; TIFF → `libtiff` (usually
  auto-pulled). Install with `--no-install-recommends`.
- **policy.xml (MANDATORY on Debian):** defaults disable PS/EPS/PDF coders and cap resources.
  Path differs by major: IM6 `/etc/ImageMagick-6/policy.xml`, IM7 `/etc/ImageMagick-7/`. We
  COPY our own policy.xml unlocking `PDF/PS/EPS/PSD/AI` (`rights="read|write"`) and setting
  sane limits for a low-power box (memory 1GiB, area 256MP, disk 4GiB, time 300s, thread 2).
  Keep the non-coder hardening (MVG/MSL/url) as shipped.
- **Use subprocess to the CLI**, not Wand (same native libs + policy apply; subprocess isolates
  crashes; one tool family shared with archives). Always pass `timeout`, capture stderr.

## 8. Archive tools
`unzip`, `p7zip-full` (`7z`), `unrar` (extract), `zip`, `tar`/`gzip`. Optional `rar` (proprietary;
opt-in build arg `ENABLE_RAR`, default OFF — `unrar` extracts but cannot create; offer 7z as the
open compression alternative). Python stdlib (`zipfile`, `tarfile`, `gzip`) covers zip/tar/gz
for-real in unit tests; `7z`/`rar`/`unrar` and ImageMagick are exercised via subprocess + the
dockerized smoke test.

## 9. Safety facts to enforce
- **zip-slip / path traversal:** before extracting any member, resolve its normalized absolute
  path and assert it stays within the destination dir (reject `..`, absolute paths, symlink
  escapes). Applies to zip, tar, 7z, rar.
- **zip-bomb:** enforce `MAX_UNCOMPRESSED_SIZE` (sum of member sizes) and `MAX_FILES` (member
  count) BEFORE/while extracting; abort + clean if exceeded. For gz (no member count), cap
  decompressed bytes streamed.
- **Never delete the user's original.** `extract` writes into a NEW subfolder beside the
  archive; the archive stays. No action ever issues a WebDAV DELETE on user content.
- **Idempotency / locking:** per-file lock keyed by fileid; skip if already processing; remove
  the trigger tag only after a verified successful upload, making it naturally re-runnable.

## Assumptions — verify later
- Reverse proxy in front of NC raises body size limit (else large PUTs 413). Documented in README.
- Service account has access (own files / shares / Group Folder) to everything it should process.
- Notifications app is enabled if the user turns on the notify feature.

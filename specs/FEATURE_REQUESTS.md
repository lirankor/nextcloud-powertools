# Feature requests — backlog tracker

Single source of truth for post-v1 feature requests. v1 = M1–M4 (extract / zip / rar(opt-in) /
render-png / render via tag, webhook + polling, Docker + ghcr CI). Status legend:
⬜ backlog · 🟡 in progress · ✅ done · ❓ needs clarification before build.

A recurring theme across these: they should all work at the **directory level** (tag a folder →
act on every matching file below it). The directory-walk + per-file output-mapping infrastructure
built for **F1** is the shared foundation F2/F3 reuse.

---

## F1 — Directory-level render (PSD→image on a tagged folder)  ✅ done
**Requested:** 2026-06-15. **Shipped:** 2026-06-15 (M5). Done after M4 CI green.
Make `render-png` / `render` (and any future render-registry source type) also work when a
**directory** is tagged: recursively render every file below it whose extension is registered in
`RENDERERS` (PSD today). Non-renderable files skipped (logged). Outputs land **beside each source
file**, mirroring the tree (`Album/a.psd` → `Album/a.png`, `Album/sub/b.psd` → `Album/sub/b.png`).

Design notes:
- `RenderPngHandler`/`RenderJpgHandler.can_handle()` currently returns False for dirs — change to
  accept dirs (walk + no-op-with-log if nothing matches).
- Pipeline: for a tagged DIR + render action, download the subtree (reuse M3
  `download_dir_as_zip` → unpack locally), hand the local dir to the handler; handler walks it,
  renders each registered file, returns output paths with the relative tree preserved; pipeline
  uploads each into the tagged dir's namespace at the mirrored path. Only matching files render
  (never re-upload originals). Respect `MAX_FILES` as a cap. Never delete originals.
- Empty/no-match folder → remove trigger tag + log "nothing to render" (treat as success).

Acceptance: tag a folder with `a.psd`, `sub/b.psd`, `notes.txt` → `a.png` + `sub/b.png` appear,
`notes.txt` untouched, trigger tag removed. Unit tests (walk + output mapping, mocked) + a real
dockerized smoke on a 2-level PSD tree. ruff/mypy clean; existing tests green.

---

## F2 — Raw photo → JPG  ✅ done (directory-level)
**Requested:** 2026-06-15. **Shipped:** 2026-06-15 (M6).
Camera raws (CR2/CR3/NEF/ARW/DNG/RAF/ORF/RW2/PEF/SRW) render to JPG via `render` and PNG via
`render-png` — no new tag; registered as renderers in the registry so the F1 directory walk works
automatically. Two-stage pipeline: `dcraw_emu -w -o 1 -q 3 -T -Z <scratch>/decoded.tiff <src>` then
`convert <tiff> -auto-orient -colorspace sRGB -depth 8 [-quality 90] <out>`. The renderer contract
was generalized to a callable `render(src, out, fmt, scratch) -> None` (was: returns an argv list)
so multi-stage conversions compose cleanly; PSD refactored to the new signature. `libraw-bin` added
to the Dockerfile runtime; `dcraw_emu` added to selftest `OPTIONAL_TOOLS`. Bypasses ImageMagick
policy.xml (IM only opens the decoded TIFF). Verified via dockerized smoke on a real Canon CR2.

### Original notes
**Requested:** 2026-06-15.
Convert camera **raw** files to JPG. Should work on a single file AND a tagged directory (reuse
F1's walk). Implemented as new entries in the **render registry** (`@renderer` decorator) so it
composes with F1 automatically.

Open design notes (confirm at research time):
- Raw formats to cover: CR2/CR3 (Canon), NEF (Nikon), ARW (Sony), DNG (Adobe), RAF (Fuji), ORF
  (Olympus), RW2 (Panasonic), PEF, SRW, … — start with the common set, extensible.
- Tooling: ImageMagick alone doesn't decode most raws well; needs a delegate — **libraw**
  (`dcraw_emu` / `libraw-bin`) or `ufraw-batch`, or pipe `dcraw`→ImageMagick. Decide the cleanest
  Debian-slim install (likely `libraw-bin`) and the exact command (preserve embedded orientation;
  reasonable quality/`-quality 90`; sRGB output). Add the apt package to the Dockerfile + selftest
  optional-tools list.
- Probably its own tag/action (`render` already = JPG; raw→JPG could just be `render` extended to
  raw exts, since target is JPG). Likely: just register raw exts in the JPG renderer path. Confirm
  whether a separate tag is wanted.

---

## F3 — `g-convert`: Google-format files → normal Office files  ❓ needs clarification (directory-level)
**Requested:** 2026-06-15.
Convert "gcloud" files (Google Docs/Sheets/Slides) to standard Office formats
(docx / xlsx / pptx). New action `g-convert`. Should work on a single file AND a tagged directory
(reuse F1's walk).

Questions to resolve BEFORE building (ask the owner at research time):
- **What is actually on disk?** After the Google migration, are these (a) rclone `.gdoc`/`.gsheet`/
  `.gslides` JSON pointer stubs (which contain only a URL — NOT convertible offline), (b) already
  exported ODF (`.odt`/`.ods`/`.odp`), or (c) Microsoft/Google exported blobs? This determines
  whether offline conversion is even possible. Pointer stubs would require re-export via Google
  APIs (out of scope / needs Google auth) — flag this.
- If they're ODF or other office-ish formats: convert via **LibreOffice headless**
  (`soffice --headless --convert-to docx --outdir … file.odt`). Needs `libreoffice` (large; maybe a
  separate/opt-in image variant given size on a low-power box) — discuss image-size tradeoff.
- Target mapping: Docs→docx, Sheets→xlsx, Slides→pptx (configurable?).
- Likely a NEW handler family (not the render registry) — a `convert` registry keyed by source ext
  → (tool, target). Keep it extensible like the render registry.

---

## F4 — More render source types (afphoto, TIFF, AI/EPS/PDF, HEIC, SVG, …)  ✅ done (directory-level)
**Requested:** 2026-06-15. **Shipped:** 2026-06-15 (M8).
Extended the render registry beyond PSD + raw to many more "files Nextcloud can't preview".
Registered (all compose with the F1 directory walk automatically):
- **convert-native raster:** `tiff`/`tif`, `bmp`, `gif`, `ico`, `tga`, `dds`, `xcf`, `jp2`/`j2k`/
  `jpc`/`jpf`, `heic`/`heif`/`hif`, `avif`, `webp` — `convert "src[0]" out` (PNG keeps alpha; JPG
  `-background white -flatten -quality 90`). `[0]` picks the first frame/page.
- **vector/page:** `pdf`, `ai`, `eps`, `ps` — same but `-density 150` BEFORE the input for crisp
  rasterization (policy.xml unlocks the coders).
- **SVG:** `svg`, `svgz` — `rsvg-convert` (IM has no SVG delegate): PNG via `rsvg-convert -o`,
  JPG via `rsvg-convert src | convert png:- -background white -flatten -quality 90 out`.
- **Affinity (best-effort):** `afphoto`, `afdesign`, `afpub`, `aftemplate`, `af` — pure-Python
  carver extracts the **largest embedded PNG preview** (Serif bakes one in); PNG target writes it
  verbatim, JPG target converts it. **Low-res preview, NOT a full render**; raises if no embedded
  PNG. CorelDraw/EMF remain unsupported.

**ZERO new apt packages** (HEIC/WEBP/JP2/ghostscript/librsvg already in the image). Only change to
ops: `rsvg-convert` added to selftest `OPTIONAL_TOOLS`. Verified via dockerized smoke (TIFF, PDF,
HEIC, WEBP, SVG through the real handler + the Affinity carver on a synthetic embedded-PNG file).

### Original notes
**Requested:** 2026-06-15.
Extend the render registry beyond PSD + raw so more "files Nextcloud can't preview" convert to a
viewable PNG/JPG. Requested examples: **`.afphoto`** (Affinity Photo), **`.tiff`**, "and other
without preview". Works on single files AND folders automatically (F1 walk) — these are just new
`@renderer` registry entries (+ any delegate package in the Dockerfile).

Likely buckets (pending the research-formats agent):
- **Easy wins, delegate already installed:** TIFF (libtiff), AI/EPS/PS/PDF (ghostscript, policy
  already unlocks them), SVG (librsvg — may need two-stage rsvg→PNG→convert for JPG), HEIC
  (libheif1 — confirm IM6 bookworm delegate), BMP/GIF/WEBP/ICO/TGA (IM native). Register the exts;
  maybe add one small package.
- **Hard / likely NOT offline-doable:** **`.afphoto`/`.afdesign`/`.afpub`** are proprietary Serif
  formats — ImageMagick can't read them. Best case is carving an embedded preview thumbnail; if not
  reliable, document as unsupported (like F3 stubs). Research agent is determining feasibility.

Plan: research → register easy-win exts (real dockerized smoke per format) → for afphoto, either
implement embedded-preview extraction if a reliable method exists, or document unsupported.

---

## F5 — `shred`: guarded permanent delete (file + dir)  ✅ done (DESTRUCTIVE, opt-in)
**Requested:** 2026-06-16. **Shipped:** 2026-06-16 (M9). Mock-verified; owner MUST live-validate.
A deliberately destructive action — the ONE feature that breaks the tool's "never delete user
content" invariant, so it is the most heavily guarded. Owner's locked decisions:
- **Behavior:** **permanent purge** — best-effort overwrite → WebDAV `DELETE` → empty from NC
  **trash** → purge **file versions**. (NOT forensically secure: storage layer + **Kopia/hetzbox
  backups** still retain the data — document loudly.)
- **Anti-accident = generated-artifact handshake + second tag (owner's idea):**
  1. Tag target with **`shred`** → worker writes a `CONFIRM-SHRED-<…>.md` receipt beside it (path,
     size, file count, warning, machine-readable target ref in front-matter) and **removes the
     `shred` tag**. No deletion yet.
  2. Owner adds **`shred-confirm`** to that confirmation file → worker reads the target ref,
     re-validates scope, performs the permanent purge, writes a receipt, notifies, audit-logs.
  3. Removing the tag / deleting the confirmation file cancels. (Stale unconfirmed requests just
     sit; optional TTL.)
- **Guardrails:** opt-in `ENABLE_SHRED=false` (default OFF); only operates **inside a designated
  `SHRED_DIR`** AND **within the service account's own namespace** (refuse SHRED_DIR root, account
  root, anything outside, shared mounts); always OCS-notify + structured audit log every step.
- **Tags configurable** (`shred` / `shred-confirm`). New handler family (not the render registry).

NEEDS (research agent, in progress): exact NC 33 WebDAV endpoints for (a) DELETE→trash vs the
trashbin app (`/remote.php/dav/trashbin/{user}/trash`: PROPFIND list + DELETE item + empty-all),
(b) file-versions purge (`/remote.php/dav/versions/{user}/versions/{fileid}`), (c) whether a plain
files DELETE goes to trash by default. Then: spec → dev agent → mock + dockerized smoke → CI.
Owner must live-validate (destructive — agents can't).

## Process
- New requests get appended here with date + status. Build order is F1 → F2 → F4 → F5; F3 gated on
  the owner's disk-check (stub vs real file).
- Each feature, when built, follows the same dark-factory rhythm: spec → dev agent → verify
  (incl. real dockerized smoke for binary-backed tools) → CI green → mark ✅ here + in PLAN.md.

## F6 — `immich` / `immich-<album>`: push photos to Immich  🟡 researching (parameterized tag)
**Requested:** 2026-06-16.
New integration power tool — upload Nextcloud photos (file OR tagged directory) into a separate
**Immich** server via its REST API. Non-destructive (NC original kept; trigger tag removed).
- **`immich`** → upload to Immich main timeline/library, NO album.
- **`immich-<album-name>`** → find-or-create album `<album-name>`, upload + add asset(s). Album name
  = everything after the first `-` (spaces allowed).
- **Directory** → walk, upload all media files (respect MAX_FILES); for `immich-<album>` all go in
  the album.

NEW MECHANISM — **parameterized/prefix trigger tags.** Unlike all prior fixed-name tags, the album
variant embeds a parameter. Extend trigger-matching: list system tags, match `immich` exactly +
`immich-*` by prefix, parse the album from the suffix. Reusable for future parameterized tools.

Flow: WebDAV download → Immich API upload (checksum dedup = idempotent) → create/add album →
remove trigger tag. Opt-in `ENABLE_IMMICH=false`; config `IMMICH_URL`, `IMMICH_API_KEY` (per-user
API key, `x-api-key`). New handler/service family (not the render registry).

NEEDS (research agent, in progress): exact current Immich API — asset upload endpoint + required
multipart fields (deviceAssetId/deviceId/fileCreatedAt/fileModifiedAt), checksum/dedup response,
album list/create/add-assets endpoints, API-key auth, server-version/ping probe. Then spec → dev
agent → mock + docker smoke → CI. Owner live-validates against the real Immich.

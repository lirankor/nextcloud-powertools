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

## F4 — More render source types (afphoto, TIFF, AI/EPS/PDF, HEIC, SVG, …)  🟡 researching (directory-level)
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

## Process
- New requests get appended here with date + status. Build order is F1 → F2 → F4 (render exts) →
  F3 (F1 unblocks the directory infra; F3 is gated on the clarification above).
- Each feature, when built, follows the same dark-factory rhythm: spec → dev agent → verify
  (incl. real dockerized smoke for binary-backed tools) → CI green → mark ✅ here + in PLAN.md.

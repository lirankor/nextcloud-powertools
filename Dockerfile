# syntax=docker/dockerfile:1
#
# nextcloud-powertools — multi-stage, slim, non-root image.
#
# Stage 1 (builder): build a wheel for the package + its pinned deps into a venv.
# Stage 2 (runtime): copy the venv, install only the runtime CLI tools the
# handlers shell out to, drop to a non-root user.
#
# Arch-agnostic: no hardcoded architecture. buildx supplies TARGETARCH for
# multi-arch (amd64 + arm64) builds in CI; nothing here depends on it.

ARG PYTHON_VERSION=3.12

# ---------------------------------------------------------------------------
# Stage 1 — builder: produce a self-contained virtualenv at /opt/venv
# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim-bookworm AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

# Build tooling (only needed to compile any wheels that lack manylinux builds,
# e.g. on arm64). Kept in this throwaway stage so the runtime image stays lean.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"

WORKDIR /build

# Install dependencies first (better layer caching), then the package itself.
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --upgrade pip \
    && pip install .

# ---------------------------------------------------------------------------
# Stage 2 — runtime
# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim-bookworm AS runtime

# Opt-in proprietary `rar` *creation* binary. OFF by default: `rar` is non-free
# and not in Debian main. `unrar-free` (extraction) ships by default; the `rar`
# action stays disabled unless you build with --build-arg ENABLE_RAR=true AND
# set ENABLE_RAR=true at runtime. 7z is the open compression alternative.
ARG ENABLE_RAR=false

ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    WORK_DIR=/tmp/ncpowertools

# Runtime CLI tools the handlers invoke via subprocess (CONTEXT.md §7/§8):
#   imagemagick                 -> magick/convert (PSD native; IM6 on bookworm)
#   ghostscript                 -> PDF/PS/EPS/AI delegate for ImageMagick
#   librsvg2-bin                -> SVG delegate (rsvg-convert) for extending render
#   libheif1                    -> HEIC delegate
#   p7zip-full                  -> 7z (extract .7z + the open compress alternative)
#   unrar-free                  -> unrar (extract .rar; see ENABLE_RAR note re: creation)
#   zip/unzip/tar/gzip/xz-utils -> archive extract/compress
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        imagemagick \
        ghostscript \
        librsvg2-bin \
        libheif1 \
        p7zip-full \
        unrar-free \
        zip \
        unzip \
        tar \
        gzip \
        xz-utils \
    && rm -rf /var/lib/apt/lists/*

# Optional: proprietary `rar` creation binary, only when explicitly opted in.
# Pulled from the Debian non-free component for the build's architecture.
RUN if [ "${ENABLE_RAR}" = "true" ]; then \
        set -eux; \
        echo "deb http://deb.debian.org/debian bookworm non-free non-free-firmware" \
            > /etc/apt/sources.list.d/nonfree.list; \
        apt-get update; \
        apt-get install -y --no-install-recommends rar; \
        rm -rf /var/lib/apt/lists/*; \
    else \
        echo "ENABLE_RAR=false -> proprietary rar NOT installed (default)"; \
    fi

# ImageMagick policy.xml: unlock PDF/PS/EPS/PSD/AI coders + sane resource caps.
# bookworm ships IM6, so the active path is /etc/ImageMagick-6/. We also drop a
# copy at the IM7 path so a future base bump keeps working.
COPY policy.xml /etc/ImageMagick-6/policy.xml
RUN mkdir -p /etc/ImageMagick-7 \
    && cp /etc/ImageMagick-6/policy.xml /etc/ImageMagick-7/policy.xml

# Copy the prebuilt virtualenv (package + deps) from the builder stage.
COPY --from=builder /opt/venv /opt/venv

# Non-root runtime user. WORK_DIR must be writable by it.
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin app \
    && mkdir -p /tmp/ncpowertools \
    && chown -R app:app /tmp/ncpowertools

WORKDIR /app
USER app

EXPOSE 8080

# Tool-presence selftest as the healthcheck: it runs the local CLI-tool phase
# and is tolerant of an unreachable Nextcloud (the NC phase is a separate
# failure domain), so it won't flap on transient NC outages. A non-zero exit
# means a REQUIRED tool is missing OR (only when NC creds are valid) NC checks
# failed; both are worth surfacing. Without creds, the NC phase short-circuits.
HEALTHCHECK --interval=60s --timeout=20s --start-period=15s --retries=3 \
    CMD ["python", "-m", "ncpowertools", "selftest"]

CMD ["python", "-m", "ncpowertools", "run"]

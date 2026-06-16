"""Application configuration via pydantic-settings.

Reads from environment + an optional ``.env`` file. Every variable in
ARCHITECTURE.md's config table is represented with its documented default.

``TAG_ACTIONS`` accepts either a JSON object string (``{"extract":"extract"}``)
or the compact ``tag:action,tag:action`` form. ``NC_ADMIN_USER``/
``NC_ADMIN_PASSWORD`` default to ``NC_USER``/``NC_APP_PASSWORD`` and
``TARGET_USER`` defaults to ``NC_USER`` (resolved post-init).
"""

from __future__ import annotations

import json

from pydantic import Field, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .errors import ConfigError

DEFAULT_TAG_ACTIONS: dict[str, str] = {
    "extract": "extract",
    "zip": "zip",
    "rar": "rar",
    "render-png": "render-png",
    "render": "render",
}


def parse_tag_actions(value: object) -> dict[str, str]:
    """Parse a TAG_ACTIONS value from JSON-object or ``tag:action,...`` form."""
    if isinstance(value, dict):
        return {str(k).strip(): str(v).strip() for k, v in value.items()}
    if not isinstance(value, str):
        raise ConfigError(f"TAG_ACTIONS must be a string or mapping, got {type(value).__name__}")
    text = value.strip()
    if not text:
        return dict(DEFAULT_TAG_ACTIONS)
    if text.startswith("{"):
        try:
            obj = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"TAG_ACTIONS is not valid JSON: {exc}") from exc
        if not isinstance(obj, dict):
            raise ConfigError("TAG_ACTIONS JSON must be an object (tag -> action)")
        return {str(k).strip(): str(v).strip() for k, v in obj.items()}
    # Compact form: tag:action,tag:action
    result: dict[str, str] = {}
    for pair in text.split(","):
        pair = pair.strip()
        if not pair:
            continue
        if ":" not in pair:
            raise ConfigError(
                f"TAG_ACTIONS entry {pair!r} is not in 'tag:action' form"
            )
        tag, action = pair.split(":", 1)
        tag, action = tag.strip(), action.strip()
        if not tag or not action:
            raise ConfigError(f"TAG_ACTIONS entry {pair!r} has an empty tag or action")
        result[tag] = action
    if not result:
        return dict(DEFAULT_TAG_ACTIONS)
    return result


def immich_album_from_tag(tag_name: str, immich_tag: str) -> str | None:
    """Parse the Immich album from a trigger tag name (the prefix mechanism).

    The exact tag ``<immich_tag>`` -> ``None`` (main library, no album). A tag
    ``<immich_tag>-<album>`` -> ``<album>`` (everything after the FIRST ``-``,
    spaces preserved). An empty suffix (``<immich_tag>-``) -> ``None``. Returns
    the album string, or ``None`` when the tag selects no album. **The caller
    must already have established (via** :func:`is_immich_tag` **) that the tag is
    an immich-pattern tag** — this only parses the suffix.
    """
    if tag_name == immich_tag:
        return None
    prefix = immich_tag + "-"
    if tag_name.startswith(prefix):
        album = tag_name[len(prefix):]
        return album or None
    return None


def is_immich_tag(tag_name: str, immich_tag: str) -> bool:
    """Whether ``tag_name`` is an immich-pattern trigger tag.

    True for the exact ``<immich_tag>`` and any ``<immich_tag>-<suffix>`` (the
    parameterized/prefix trigger-tag mechanism, F6). ``<immich_tag>-`` (empty
    suffix) still matches the action (just selects no album).
    """
    return tag_name == immich_tag or tag_name.startswith(immich_tag + "-")


class Settings(BaseSettings):
    """Runtime configuration. Instantiate via :func:`load_settings`."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- required ---
    NEXTCLOUD_URL: str
    NC_USER: str
    NC_APP_PASSWORD: str

    # --- webhook ---
    WEBHOOK_SECRET: str = ""
    WEBHOOK_HEADER: str = "Authorization"
    WEBHOOK_PATH: str = "/nc-hook"
    WEBHOOK_HOST: str = "0.0.0.0"  # intentional bind-all inside container
    WEBHOOK_PORT: int = 8080

    # --- behavior ---
    TAG_ACTIONS: dict[str, str] = Field(default_factory=lambda: dict(DEFAULT_TAG_ACTIONS))
    ERROR_TAG: str = "powertools-error"
    ENABLE_RAR: bool = False
    POLL_INTERVAL: int = 60

    # --- shred (DESTRUCTIVE, opt-in; see README "⚠️ Shred") ---
    # When false (default) the shred actions are NOT registered and any shred
    # tag is ignored. Turning this on enables a permanent purge-from-Nextcloud.
    ENABLE_SHRED: bool = False
    # Path (relative to the user namespace root) shred is strictly confined to.
    # Shred refuses anything that is not strictly INSIDE this folder.
    SHRED_DIR: str = "Shredder"
    # Trigger tags for the two-step handshake.
    SHRED_TAG: str = "shred"
    SHRED_CONFIRM_TAG: str = "shred-confirm"

    # --- immich (opt-in; see README "Immich integration") ---
    # When false (default) the immich action is NOT registered and any immich /
    # immich-<album> tag is IGNORED. Turning this on enables uploading a COPY of
    # tagged photos/videos to a separate Immich server (NC original is kept).
    ENABLE_IMMICH: bool = False
    # Base URL of the Immich server, e.g. https://immich.example.com (no trailing
    # /api). The service talks to ``<IMMICH_URL>/api/...``.
    IMMICH_URL: str = ""
    # Per-user Immich API key (Account Settings -> API Keys). Sent as x-api-key.
    IMMICH_API_KEY: str = ""
    # Stable, constant device id reported on every upload (groups all uploads as
    # one "device" in Immich).
    IMMICH_DEVICE_ID: str = "nextcloud-powertools"
    # Base trigger tag. ``<IMMICH_TAG>`` (exact) uploads to the main library; any
    # ``<IMMICH_TAG>-<album>`` tag uploads + adds to album ``<album>``.
    IMMICH_TAG: str = "immich"

    # --- safety limits ---
    MAX_UNCOMPRESSED_SIZE: int = 2147483648  # 2 GiB
    MAX_FILES: int = 10000

    # --- misc ---
    WORK_DIR: str = "/tmp/ncpowertools"  # container scratch dir
    LOG_LEVEL: str = "INFO"
    NOTIFY: bool = False

    # --- admin / target (default to the service account; resolved below) ---
    NC_ADMIN_USER: str = ""
    NC_ADMIN_PASSWORD: str = ""
    TARGET_USER: str = ""

    @field_validator("TAG_ACTIONS", mode="before")
    @classmethod
    def _parse_tag_actions(cls, value: object) -> dict[str, str]:
        return parse_tag_actions(value)

    @field_validator("NEXTCLOUD_URL")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @model_validator(mode="after")
    def _apply_defaults(self) -> Settings:
        if not self.NC_ADMIN_USER:
            self.NC_ADMIN_USER = self.NC_USER
        if not self.NC_ADMIN_PASSWORD:
            self.NC_ADMIN_PASSWORD = self.NC_APP_PASSWORD
        if not self.TARGET_USER:
            self.TARGET_USER = self.NC_USER
        # When shred is enabled, its two tags become active trigger tags so the
        # poller searches them and the pipeline routes them. When disabled they
        # are deliberately absent — a shred tag is then never even searched for.
        if self.ENABLE_SHRED:
            self.TAG_ACTIONS.setdefault(self.SHRED_TAG, "shred")
            self.TAG_ACTIONS.setdefault(self.SHRED_CONFIRM_TAG, "shred-confirm")
        return self


def load_settings(**overrides: object) -> Settings:
    """Load settings, raising :class:`ConfigError` with a clear message on failure.

    Wraps pydantic's ``ValidationError`` so the rest of the app deals only with
    our typed ``ConfigError`` (required-var messages included).
    """
    try:
        return Settings(**overrides)  # type: ignore[arg-type]
    except ValidationError as exc:
        missing = [
            ".".join(str(p) for p in err["loc"])
            for err in exc.errors()
            if err["type"] == "missing"
        ]
        if missing:
            raise ConfigError(
                "Missing required configuration: "
                + ", ".join(missing)
                + " (set them in the environment or a .env file)"
            ) from exc
        raise ConfigError(f"Invalid configuration: {exc}") from exc

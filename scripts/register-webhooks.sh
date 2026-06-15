#!/usr/bin/env bash
#
# register-webhooks.sh — register / list / delete the tag-assignment webhook in
# Nextcloud's `webhook_listeners` app, pointed at this worker.
#
# Webhooks require NC >= 32 AND the `webhook_listeners` app enabled. The worker
# also works on NC >= 30 via polling (POLL_INTERVAL) with no webhook at all —
# this script is only the low-latency NC32+ enhancement.
#
# Outgoing auth is a STATIC header (no HMAC in the official app): we register
# authMethod="header", authData={"Authorization":"Bearer <WEBHOOK_SECRET>"}.
# The worker compares it constant-time and MUST be served over TLS.
#
# Usage:
#   register-webhooks.sh register          # create the TagAssignedEvent webhook
#   register-webhooks.sh --list            # list registered webhooks
#   register-webhooks.sh --delete <id>     # delete a webhook by id
#
# Config via env (or flags below):
#   NEXTCLOUD_URL     e.g. https://cloud.example.com   (--url)
#   NC_ADMIN_USER     admin/service account             (--user)
#   NC_ADMIN_PASSWORD admin app-password                (--password)
#   WEBHOOK_URL       public worker callback URL        (--webhook-url)
#                     e.g. https://worker.example.com/nc-hook
#   WEBHOOK_SECRET    shared secret (Bearer token)      (--secret)
#
# A `.env` in the current dir is auto-sourced if present.
set -euo pipefail

usage() {
  sed -n '2,33p' "$0" | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

# --- Load .env if present (without clobbering already-exported vars) ---
if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

NEXTCLOUD_URL="${NEXTCLOUD_URL:-}"
NC_ADMIN_USER="${NC_ADMIN_USER:-${NC_USER:-}}"
NC_ADMIN_PASSWORD="${NC_ADMIN_PASSWORD:-${NC_APP_PASSWORD:-}}"
WEBHOOK_URL="${WEBHOOK_URL:-}"
WEBHOOK_SECRET="${WEBHOOK_SECRET:-}"

ACTION=""
DELETE_ID=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    register)            ACTION="register"; shift ;;
    --list|-l)           ACTION="list"; shift ;;
    --delete|-d)         ACTION="delete"; DELETE_ID="${2:-}"; shift 2 ;;
    --url)               NEXTCLOUD_URL="${2:-}"; shift 2 ;;
    --user)              NC_ADMIN_USER="${2:-}"; shift 2 ;;
    --password)          NC_ADMIN_PASSWORD="${2:-}"; shift 2 ;;
    --webhook-url)       WEBHOOK_URL="${2:-}"; shift 2 ;;
    --secret)            WEBHOOK_SECRET="${2:-}"; shift 2 ;;
    -h|--help)           usage 0 ;;
    *) echo "unknown argument: $1" >&2; usage 1 ;;
  esac
done

[[ -z "$ACTION" ]] && { echo "no action given" >&2; usage 1; }

die() { echo "error: $*" >&2; exit 1; }

[[ -z "$NEXTCLOUD_URL" ]] && die "NEXTCLOUD_URL (or --url) is required"
[[ -z "$NC_ADMIN_USER" ]] && die "NC_ADMIN_USER (or --user) is required"
[[ -z "$NC_ADMIN_PASSWORD" ]] && die "NC_ADMIN_PASSWORD (or --password) is required"

BASE="${NEXTCLOUD_URL%/}"
OCS="$BASE/ocs/v2.php/apps/webhook_listeners/api/v1/webhooks"
AUTH=(-u "${NC_ADMIN_USER}:${NC_ADMIN_PASSWORD}")
HDRS=(-H 'OCS-APIRequest: true' -H 'Accept: application/json')

# --- Version probe: warn if NC major < 32 (tag webhooks won't fire) ---
probe_version() {
  local caps major
  caps="$(curl -fsS "${AUTH[@]}" "${HDRS[@]}" \
    "$BASE/ocs/v2.php/cloud/capabilities?format=json" 2>/dev/null || true)"
  if [[ -z "$caps" ]]; then
    echo "warning: could not probe NC capabilities (auth/URL?); continuing" >&2
    return
  fi
  # Pull ocs.data.version.major without requiring jq.
  major="$(printf '%s' "$caps" | grep -o '"major"[[:space:]]*:[[:space:]]*[0-9]*' \
    | head -n1 | grep -o '[0-9]*$' || true)"
  if [[ -n "$major" ]]; then
    echo "Nextcloud major version: $major"
    if (( major < 32 )); then
      echo "WARNING: NC < 32 — tag-assignment webhooks will NOT fire." >&2
      echo "         Rely on polling instead (set POLL_INTERVAL > 0)." >&2
    fi
  fi
}

case "$ACTION" in
  list)
    probe_version
    echo "Registered webhooks:"
    curl -fsS "${AUTH[@]}" "${HDRS[@]}" "$OCS" || die "list failed"
    echo
    echo "Tip: 'occ webhook_listeners:list --output=json_pretty' shows the same"
    echo "list from the NC server side (create/delete are OCS-only, as used here)."
    ;;

  delete)
    [[ -z "$DELETE_ID" ]] && die "--delete requires a webhook id"
    curl -fsS "${AUTH[@]}" "${HDRS[@]}" -X DELETE "$OCS/$DELETE_ID" \
      || die "delete failed"
    echo
    echo "deleted webhook $DELETE_ID"
    ;;

  register)
    [[ -z "$WEBHOOK_URL" ]] && die "WEBHOOK_URL (or --webhook-url) is required to register"
    [[ -z "$WEBHOOK_SECRET" ]] && die "WEBHOOK_SECRET (or --secret) is required to register"
    probe_version

    # Note the DOUBLE-backslash in the JSON FQCN: JSON needs \\ to encode the
    # single PHP namespace backslash in OCP\SystemTag\TagAssignedEvent.
    payload=$(cat <<JSON
{
  "httpMethod": "POST",
  "uri": "${WEBHOOK_URL}",
  "event": "OCP\\\\SystemTag\\\\TagAssignedEvent",
  "authMethod": "header",
  "authData": { "Authorization": "Bearer ${WEBHOOK_SECRET}" }
}
JSON
)
    echo "Registering TagAssignedEvent -> ${WEBHOOK_URL}"
    curl -fsS "${AUTH[@]}" "${HDRS[@]}" -H 'Content-Type: application/json' \
      -X POST "$OCS" -d "$payload" || die "register failed"
    echo
    echo "Done. Verify with: $0 --list"
    echo "Reminder: the worker must be reachable at WEBHOOK_URL over TLS, and"
    echo "WEBHOOK_SECRET here must match the worker's WEBHOOK_SECRET env."
    ;;

  *) usage 1 ;;
esac

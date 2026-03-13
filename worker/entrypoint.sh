#!/usr/bin/env bash
set -euo pipefail

STATE_DIR="${STATE_DIR:-/state}"
SUCCESS_MARKER="${STATE_DIR}/success_notified"
NOTIFY_BACKEND="${NOTIFY_BACKEND:-none}"
UNRAID_NOTIFY_CMD="${UNRAID_NOTIFY_CMD:-/usr/local/bin/unraid-notify}"
NOTIFY_WEBHOOK_URL="${NOTIFY_WEBHOOK_URL:-}"

notify_success() {
  local message="OCI free-tier target profile reached for compartment ${COMPARTMENT_NAME}."

  case "$NOTIFY_BACKEND" in
    none)
      echo "[notify] backend=none, skipping notification"
      ;;
    unraid)
      if [[ -x "$UNRAID_NOTIFY_CMD" ]]; then
        "$UNRAID_NOTIFY_CMD" \
          -e "OCI Free Tier" \
          -s "OCI capacity acquired" \
          -d "Target profile reached" \
          -i "normal" \
          -m "$message"
      else
        echo "[notify] UNRAID_NOTIFY_CMD not executable: $UNRAID_NOTIFY_CMD"
      fi
      ;;
    webhook)
      if [[ -n "$NOTIFY_WEBHOOK_URL" ]]; then
        curl -fsS -X POST -H 'Content-Type: application/json' \
          -d "{\"event\":\"oci_capacity_acquired\",\"message\":\"$message\"}" \
          "$NOTIFY_WEBHOOK_URL" >/dev/null || echo "[notify] webhook delivery failed"
      else
        echo "[notify] NOTIFY_WEBHOOK_URL empty"
      fi
      ;;
    *)
      echo "[notify] unknown backend: $NOTIFY_BACKEND"
      ;;
  esac
}

mkdir -p "$STATE_DIR"

if [[ -f "$SUCCESS_MARKER" ]]; then
  echo "[entrypoint] success marker already present: $SUCCESS_MARKER"
  exec tail -f /dev/null
fi

# Backward-compatible profile path resolution:
# - legacy env: VM_PROFILE_FILE
# - current env: PROFILE_DEFAULTS_FILE
VM_PROFILE_FILE="${VM_PROFILE_FILE:-${PROFILE_DEFAULTS_FILE:-}}"
if [[ -z "${VM_PROFILE_FILE}" ]]; then
  echo "[entrypoint] missing VM_PROFILE_FILE/PROFILE_DEFAULTS_FILE"
  exit 1
fi

python3 /app/worker/provision_free_tier_retry.py \
  --profile "${OCI_PROFILE}" \
  --compartment-name "${COMPARTMENT_NAME}" \
  --ssh-key-file "${SSH_KEY_FILE}" \
  --retry-seconds "${RETRY_SECONDS}" \
  --max-attempts "${MAX_ATTEMPTS}" \
  --vm-profile-file "${VM_PROFILE_FILE}"

rc=$?
if [[ $rc -eq 0 ]]; then
  notify_success
  touch "$SUCCESS_MARKER"
  exec tail -f /dev/null
fi

exit $rc

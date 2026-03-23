#!/usr/bin/env bash
set -euo pipefail

STATE_DIR="${STATE_DIR:-/app/state}"
SUCCESS_MARKER="${STATE_DIR}/success_notified"
NOTIFY_BACKEND="${NOTIFY_BACKEND:-none}"
UNRAID_NOTIFY_CMD="${UNRAID_NOTIFY_CMD:-/usr/local/bin/unraid-notify}"
NOTIFY_WEBHOOK_URL="${NOTIFY_WEBHOOK_URL:-}"

notify_success() {
  local message="OCI free-tier target profile reached for all accounts."

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

notify_failure() {
  local message="$1"

  case "$NOTIFY_BACKEND" in
    none)
      echo "[notify] backend=none, skipping failure notification"
      ;;
    unraid)
      if [[ -x "$UNRAID_NOTIFY_CMD" ]]; then
        "$UNRAID_NOTIFY_CMD" \
          -e "OCI Free Tier" \
          -s "OCI watcher error" \
          -d "$message" \
          -i "warning" \
          -m "$message"
      else
        echo "[notify] UNRAID_NOTIFY_CMD not executable: $UNRAID_NOTIFY_CMD"
      fi
      ;;
    webhook)
      if [[ -n "$NOTIFY_WEBHOOK_URL" ]]; then
        curl -fsS -X POST -H 'Content-Type: application/json' \
          -d "{\"event\":\"oci_watcher_error\",\"message\":\"$message\"}" \
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

if python3 /app/worker/provision_free_tier_retry.py "$@"; then
  rc=0
else
  rc=$?
fi
if [[ $rc -eq 0 ]]; then
  notify_success
  touch "$SUCCESS_MARKER"
  exec tail -f /dev/null
fi

notify_failure "Watcher exited with code ${rc}. Check container logs for OCI error classification."
exit $rc

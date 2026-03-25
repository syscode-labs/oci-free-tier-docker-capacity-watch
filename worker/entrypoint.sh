#!/usr/bin/env bash
set -euo pipefail

STATE_DIR="${STATE_DIR:-/app/state}"
SUCCESS_MARKER="${STATE_DIR}/success_notified"
NOTIFY_BACKEND="${NOTIFY_BACKEND:-none}"
UNRAID_NOTIFY_CMD="${UNRAID_NOTIFY_CMD:-/usr/local/bin/unraid-notify}"
NOTIFY_WEBHOOK_URL="${NOTIFY_WEBHOOK_URL:-}"
TELEGRAM_BOT_TOKEN="${TELEGRAM_BOT_TOKEN:-}"
TELEGRAM_CHAT_ID="${TELEGRAM_CHAT_ID:-}"

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
    telegram)
      if [[ -n "$TELEGRAM_BOT_TOKEN" && -n "$TELEGRAM_CHAT_ID" ]]; then
        curl -fsS -X POST \
          "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
          -d "chat_id=${TELEGRAM_CHAT_ID}&text=$(python3 -c "import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1]))" "$message")" \
          >/dev/null || echo "[notify] telegram delivery failed"
      else
        echo "[notify] TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set"
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
    telegram)
      if [[ -n "$TELEGRAM_BOT_TOKEN" && -n "$TELEGRAM_CHAT_ID" ]]; then
        curl -fsS -X POST \
          "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
          -d "chat_id=${TELEGRAM_CHAT_ID}&text=$(python3 -c "import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1]))" "$message")" \
          >/dev/null || echo "[notify] telegram delivery failed"
      else
        echo "[notify] TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set"
      fi
      ;;
    *)
      echo "[notify] unknown backend: $NOTIFY_BACKEND"
      ;;
  esac
}

mkdir -p "$STATE_DIR"

if [[ -f "$SUCCESS_MARKER" ]]; then
  echo "[entrypoint] success marker present — running in bot-only mode"
  exec python3 /app/worker/provision_free_tier_retry.py "$@"
fi

python3 /app/worker/provision_free_tier_retry.py "$@"
rc=$?

if [[ $rc -ne 0 ]]; then
  notify_failure "Watcher exited with code ${rc}. Check container logs for OCI error classification."
fi
exit $rc

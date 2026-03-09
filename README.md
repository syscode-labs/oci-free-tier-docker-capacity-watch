# oci-free-tier-docker-capacity-watch
[![CI](https://github.com/syscode-labs/oci-free-tier-docker-capacity-watch/actions/workflows/ci.yml/badge.svg)](https://github.com/syscode-labs/oci-free-tier-docker-capacity-watch/actions/workflows/ci.yml)
[![Release Image](https://github.com/syscode-labs/oci-free-tier-docker-capacity-watch/actions/workflows/release-image.yml/badge.svg)](https://github.com/syscode-labs/oci-free-tier-docker-capacity-watch/actions/workflows/release-image.yml)
[![GitHub Release](https://img.shields.io/github/v/release/syscode-labs/oci-free-tier-docker-capacity-watch)](https://github.com/syscode-labs/oci-free-tier-docker-capacity-watch/releases)
[![GHCR](https://img.shields.io/badge/GHCR-ghcr.io%2Fsyscode--labs%2Foci--free--tier--docker--capacity--watch-blue)](https://ghcr.io/syscode-labs/oci-free-tier-docker-capacity-watch)

Dockerized OCI free-tier capacity watcher that retries until your target VM profile is provisioned.

## What it does

- Reads OCI credentials from a mounted `.oci` directory (read-only)
- Uses a chosen `OCI_PROFILE` from mounted config
- Creates/reuses dedicated compartment + network resources
- Retries compute launch until capacity is available
- Sends one-time success notification (optional)

## Security posture

This project is built for hostile-ish hosts with least-privilege defaults:

- No credentials in image or repo
- Credentials mounted read-only from host (`/run/oci`)
- Read-only root filesystem
- `tmpfs` for `/tmp`
- `cap_drop: [ALL]`
- `no-new-privileges`

Important: if host root is compromised, mounted credentials can still be exfiltrated. Use a scoped OCI user/key and rotate regularly.

## Notification backends

- `none` (default)
- `unraid` (mount and call Unraid notify binary)
- `webhook` (HTTP POST)

## Files

- `docker-compose.yml`
- `worker/provision_free_tier_retry.py`
- `worker/entrypoint.sh`
- `.env.example`
- `QUICKSTART.md`

See `QUICKSTART.md` for setup.

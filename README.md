# oci-free-tier-docker-capacity-watch
[![CI](https://github.com/syscode-labs/oci-free-tier-docker-capacity-watch/actions/workflows/ci.yml/badge.svg)](https://github.com/syscode-labs/oci-free-tier-docker-capacity-watch/actions/workflows/ci.yml)
[![Auto Release (Code Only)](https://github.com/syscode-labs/oci-free-tier-docker-capacity-watch/actions/workflows/auto-release-code.yml/badge.svg)](https://github.com/syscode-labs/oci-free-tier-docker-capacity-watch/actions/workflows/auto-release-code.yml)
[![Release Image](https://github.com/syscode-labs/oci-free-tier-docker-capacity-watch/actions/workflows/release-image.yml/badge.svg)](https://github.com/syscode-labs/oci-free-tier-docker-capacity-watch/actions/workflows/release-image.yml)
[![GitHub Release](https://img.shields.io/github/v/release/syscode-labs/oci-free-tier-docker-capacity-watch)](https://github.com/syscode-labs/oci-free-tier-docker-capacity-watch/releases)
[![GHCR](https://img.shields.io/badge/GHCR-ghcr.io%2Fsyscode--labs%2Foci--free--tier--docker--capacity--watch-blue)](https://ghcr.io/syscode-labs/oci-free-tier-docker-capacity-watch)

Dockerized OCI free-tier capacity watcher that retries until your target VM profile is provisioned.

## What it does

- Reads OCI credentials from a mounted `.oci` directory (read-only)
- Uses a chosen `OCI_PROFILE` from mounted config
- Creates/reuses dedicated compartment + network resources
- Uses a shared JSON profile defaults file for compute/LB behavior
- Retries compute launch until capacity is available
- Sends one-time success notification (optional)

## Profile defaults

Compute and LB profile is loaded from `profile.defaults.json` (mounted via `PROFILE_DEFAULTS_FILE`):

- A1 count/shape sizing
- Micro count/boot size
- Free LB enable flag and display name

LB bandwidth is fixed to Always Free `10 Mbps` in code.

For Always Free LB details and setup guidance, see:

- [OCI Free Tier resources (oci-free-tier-manager)](https://github.com/syscode-labs/oci-free-tier-manager/blob/main/FREE_TIER_RESOURCES.md)
- [OCI reserved IPs and load balancer notes](https://github.com/syscode-labs/oci-free-tier-manager/blob/main/docs/OCI_RESERVED_IPS_AND_LB.md)

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
- `profile.defaults.json`
- `worker/provision_free_tier_retry.py`
- `worker/entrypoint.sh`
- `.env.example`
- `Makefile`
- `.mise.toml`
- `QUICKSTART.md`

See `QUICKSTART.md` for setup.

## Task usage

First run in a new clone:

```bash
mise trust
mise env-gen
```

Useful tasks:

- `mise env-gen` - create `.env` if missing and validate compose config
- `mise env-gen-force` - recreate `.env` from template and validate
- `mise env-check` - validate local env paths/profile JSON and compose rendering
- `mise apply` - run watcher in foreground

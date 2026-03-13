# Quickstart

## 1) Prepare env

```bash
mise trust
mise env-gen
# or: make env-gen
```

Edit `.env`:

- `OCI_PROFILE` to your profile name in mounted `.oci/config`
- `WATCHER_IMAGE` to the image tag you want (default points to GHCR release)
- `CONTAINER_USER`:
  - keep `0` for maximum compatibility with strict `.oci` file permissions
  - set non-root uid:gid only if mounted files are readable by that user
- `OCI_MOUNT_DIR` to host directory containing `.oci/config` and key files
- `SSH_PUBLIC_KEY_FILE` to host public key path
- `PROFILE_DEFAULTS_FILE` to host path for the shared profile JSON
  (default file in repo: `profile.defaults.json`)
- `NOTIFY_BACKEND` and optional notification settings

For Unraid notification support:

- set `NOTIFY_BACKEND=unraid`
- set `UNRAID_NOTIFY_BIN=/usr/local/emhttp/webGui/scripts/notify`

Optional local validation:

```bash
mise env-check
# or: make env-check
```

## 2) Start watcher

```bash
docker compose up -d
```

## 3) Check logs

```bash
docker compose logs -f watcher
```

Look for:

- `Launch cycle #...`
- `Capacity unavailable ...` (normal while waiting)
- `Target profile satisfied. Provisioning complete.`

## 4) Stop

```bash
docker compose down
```

## 5) Autostart on reboot

Container restart policy is already `unless-stopped`.

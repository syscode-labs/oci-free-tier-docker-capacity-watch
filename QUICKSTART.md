# Quickstart

## 1) Configure accounts

Copy the example accounts file and fill in your OCI details:

```bash
cp accounts.json.example config/accounts.json
```

Edit `config/accounts.json`:

- Set each `profile` to match an OCI config profile in your `~/.oci/config`
- Set each `compartment_id` to the OCID of the compartment to provision into
- Set `existing_subnet_id` if you want to use a pre-existing subnet (skips VCN/networking creation)
- Override `ampere_node_names` / `micro_node_names` per account if needed
- Set `report_output` to where the import report should be written (default: `./state/<profile>-import.tf`)

Copy or edit the shared compute defaults:

```bash
cp profile.defaults.json config/profile.defaults.json
```

Edit `config/profile.defaults.json` to set OCPU/memory/boot size per instance type.

## 2) Set environment variables

```bash
export OCI_CONFIG_DIR=~/.oci     # directory containing your OCI config and key files
export SSH_KEY_FILE=~/.ssh/id_rsa.pub
export RETRY_SECONDS=300         # optional, default 300
```

## 3) Start watcher

```bash
docker compose up -d
```

## 4) Check logs

```bash
docker compose logs -f watcher
```

Look for:

- `--- Cycle #... ---` — each provisioning cycle
- `Capacity VM.Standard.A1.Flex in AD-X: unavailable` — normal while waiting
- `[profile] Targets satisfied — writing import report` — account done

## 5) Import into tofu state

When all accounts are satisfied, import reports appear in `./state/`:

```
state/fonderiadigitale-import.tf
state/syscode-homelab-import.tf
```

Drop each report into `syscode-infra-private/oci/` and run:

```bash
tofu apply -var-file=oci/<account>.tfvars -chdir=module/tofu/oci
```

## 6) Stop

```bash
docker compose down
```

Container restart policy is `unless-stopped` — it will resume on reboot.

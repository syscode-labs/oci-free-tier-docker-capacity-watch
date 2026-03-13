.DEFAULT_GOAL := env-gen

.PHONY: env-gen env-gen-force env-check env apply
env-gen:
	@if [ -f .env ]; then \
		echo ".env already exists (unchanged)"; \
	else \
		cp .env.example .env; \
		echo "Created .env from .env.example (includes commented defaults and explanations)"; \
	fi
	@docker compose config >/dev/null
	@echo "Compose configuration validated"

env-gen-force:
	@cp .env.example .env
	@echo "Recreated .env from .env.example"
	@docker compose config >/dev/null
	@echo "Compose configuration validated"

env-check:
	@set -e; \
	if [ ! -f .env ]; then \
		echo ".env missing. Run: make env-gen"; \
		exit 1; \
	fi; \
	set -a; . ./.env; set +a; \
	missing=0; \
	if [ ! -d "$$OCI_MOUNT_DIR" ]; then echo "Missing OCI_MOUNT_DIR directory: $$OCI_MOUNT_DIR"; missing=1; fi; \
	if [ ! -f "$$SSH_PUBLIC_KEY_FILE" ]; then echo "Missing SSH_PUBLIC_KEY_FILE: $$SSH_PUBLIC_KEY_FILE"; missing=1; fi; \
	if [ ! -f "$$PROFILE_DEFAULTS_FILE" ]; then echo "Missing PROFILE_DEFAULTS_FILE: $$PROFILE_DEFAULTS_FILE"; missing=1; fi; \
	if [ "$$NOTIFY_BACKEND" = "unraid" ] && [ ! -e "$$UNRAID_NOTIFY_BIN" ]; then \
		echo "Warning: UNRAID_NOTIFY_BIN not present on this host: $$UNRAID_NOTIFY_BIN"; \
	fi; \
	if [ "$$missing" -ne 0 ]; then exit 1; fi; \
	docker compose config >/dev/null; \
	python3 -c 'import json,sys; data=json.load(open(sys.argv[1], "r", encoding="utf-8")); req=["ampere_instance_count","ampere_ocpus_per_instance","ampere_memory_per_instance","ampere_boot_volume_size","micro_instance_count","micro_boot_volume_size","enable_free_lb","lb_display_name"]; missing=[k for k in req if k not in data]; assert not missing, "Missing profile keys: " + ", ".join(missing); print("Profile defaults validated")' "$$PROFILE_DEFAULTS_FILE"
	@echo "Environment check passed"

env: env-gen

apply:
	docker compose up watcher

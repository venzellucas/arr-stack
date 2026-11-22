# Run on the Docker host, from this folder.

SHELL := /bin/sh

# Use the GPU override when the host has a render device.
RENDER_GID := $(shell stat -c %g /dev/dri/renderD128 2>/dev/null)
ifneq ($(RENDER_GID),)
export RENDER_GID
export COMPOSE_FILE := docker-compose.yml:compose.gpu.yml
endif

.PHONY: up check update down logs

up:
	@sh scripts/check.sh pre
	docker compose up -d --remove-orphans
	@sh scripts/check.sh post
	@echo "HolyArrs: http://<this-host>:6262  qBittorrent: :8080  Jellyfin: :8096"

check:
	@sh scripts/check.sh all

update:
	@# Through the updater, so the same Compose version decides what changed.
	docker exec media-updater /bin/sh /stack/scripts/update.sh

down:
	docker compose down

logs:
	docker compose logs -f --tail=100

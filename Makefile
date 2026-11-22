# Run on the Docker host, from this folder. Settings come only from .env.

SHELL := /bin/sh
SETUP := docker compose run --rm -T setup

.PHONY: up check update down logs credentials test

up:
	@sh scripts/check.sh pre
	@$(SETUP) seed
	docker compose up -d --remove-orphans
	@# A profile switched off in .env does not remove its container by itself.
	@sh scripts/check.sh updater-on || docker compose --profile updater rm -sf updater >/dev/null 2>&1 || true
	@sh scripts/check.sh post
	@$(SETUP) connect

check:
	@sh scripts/check.sh all

update:
	@if sh scripts/check.sh updater-on; then \
	  docker exec media-updater /bin/sh /stack/scripts/update.sh; \
	else sh scripts/update.sh; fi

down:
	docker compose --profile updater down

logs:
	docker compose logs -f --tail=100

credentials:
	@sh scripts/check.sh credentials

test:
	docker run --rm -v "$(CURDIR)":/src:ro -w /src -e PYTHONDONTWRITEBYTECODE=1 python:3-alpine \
	  python3 -m unittest discover -s tests -v

#!/bin/sh
# Pull new images for the apps and recreate only the containers whose image
# changed. Run by the updater container on its schedule, or by `make update`.
set -eu
cd "$(dirname "$0")/.."
apps="holyarrs qbittorrent jellyfin"
echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) update: pulling $apps"
docker compose pull --quiet $apps
docker compose up -d --no-deps $apps
docker image prune -f >/dev/null
echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) update: done"

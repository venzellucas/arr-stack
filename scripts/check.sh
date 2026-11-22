#!/bin/sh
# Checks what this stack needs from the host (pre) and that the apps answer
# (post). It never installs, mounts or changes anything on the host.
set -eu
cd "$(dirname "$0")/.."

fail() { echo "FAIL: $*" >&2; exit 1; }
ok() { echo "ok:   $*"; }

# A value from .env when present, else the same default as docker-compose.yml.
setting() {
  value=$(sed -n "s/^[[:space:]]*$1=//p" .env 2>/dev/null | tail -n 1 | sed "s/^[\"']//; s/[\"']\$//")
  echo "${value:-$2}"
}

pre() {
  command -v docker >/dev/null 2>&1 || fail "Docker is not installed"
  docker compose version >/dev/null 2>&1 || fail "Docker Compose v2 ('docker compose') is missing"
  docker info >/dev/null 2>&1 || fail "cannot reach the Docker daemon; run as root or as a member of the docker group"
  ok "Docker $(docker version -f '{{.Server.Version}}'), $(docker compose version --short 2>/dev/null || echo compose)"

  data=$(setting DATA_DIR /data)
  [ -d "$data" ] || fail "$data does not exist; create it (downloads and media inside it), or set DATA_DIR in .env"
  [ -w "$data" ] || fail "$data is not writable"
  # Downloads and media must share one filesystem, or imports are full copies.
  dev=$(stat -c %d "$data")
  for dir in "$data"/*/; do
    [ -d "$dir" ] || continue
    [ "$(stat -c %d "$dir")" = "$dev" ] || fail "$dir is a separate filesystem from $data; hardlinks between downloads and media will not work"
  done
  puid=$(setting PUID 1000)
  owner=$(stat -c %u "$data")
  [ "$owner" = "$puid" ] || echo "warn: $data belongs to uid $owner, but the apps run as PUID $puid"
  ok "$data is writable and on one filesystem"
}

post() {
  for name in holyarrs qbittorrent jellyfin; do
    tries=0
    while :; do
      state=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$name" 2>/dev/null || echo missing)
      [ "$state" = healthy ] && break
      tries=$((tries + 1))
      [ "$tries" -lt 60 ] || fail "$name is $state; see: docker logs $name"
      sleep 3
    done
    ok "$name is healthy"
  done
  if updater_on; then
    [ "$(docker inspect -f '{{.State.Running}}' media-updater 2>/dev/null)" = true ] || fail "media-updater is not running"
    ok "media-updater is running (schedule: $(setting UPDATE_SCHEDULE '0 5 * * 0'))"
  else
    ok "updater is off (COMPOSE_PROFILES=updater in .env turns it on)"
  fi
}

# The updater is on when COMPOSE_PROFILES in .env lists it.
updater_on() {
  case ",$(setting COMPOSE_PROFILES '')," in *,updater,*) return 0 ;; *) return 1 ;; esac
}

credentials() {
  file="$(setting CONFIG_DIR /opt/appdata)/stack-setup/credentials.json"
  if [ -f "$file" ]; then echo "$file"; else echo "no credentials yet ($file); run make up"; fi
}

case "${1:-all}" in
  pre) pre ;;
  post) post ;;
  all) pre; post ;;
  updater-on) updater_on ;;
  credentials) credentials ;;
  *) fail "usage: $0 [pre|post|all|updater-on|credentials]" ;;
esac

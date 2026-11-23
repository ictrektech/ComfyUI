#!/usr/bin/env bash
# docker/vos-dev/vos-dev.sh — debug this source tree inside VOS without an image build.
#
# The dev container runs the installed application's own image, so Python dependencies,
# the patched frontend package and GPU support match it, and mounts this checkout over
# /src. A temporary Traefik route sends /app/com.ictrek.comfyui to that container;
# removing the route hands platform traffic back to the installed app, which keeps
# running untouched.
#
# Usage:
#   make vos-dev            start, wait for the gateway, install the route, follow the log
#   make vos-dev-down       remove the route and stop the container
#   make vos-dev-logs       follow the container log
#   make vos-dev-status     container, gateway, route and platform link state
#   make vos-dev-shell      shell inside the container
#   make vos-dev-reload     restart the gateway after backend edits
#
# Edits and how they take effect:
#   vos/*.html, vos/*.js    reload the page; the gateway serves them with no-cache
#   vos/gateway.py          make vos-dev-reload
#   comfy/, nodes.py        make vos-dev-reload; a new account also starts a new worker
#   vos/patch_frontend.py   run python3 vos/patch_frontend.py in the container, reload
#
# Environment overrides:
#   VOS_DEV_PORT(38093)              host port for the dev gateway
#   COMFYUI_DEV_STAGE_DIR            data, vendored nodes and compose state,
#                                    default ~/dev/build/ComfyUI/vos-dev
#   COMFYUI_DEV_BASE_IMAGE           image to run; defaults to the installed app's
#   COMFYUI_DEV_HEALTH_TIMEOUT(180)  seconds to wait for the gateway
#   COMFYUI_DEV_REFRESH_ENV(0)       re-read the installed app environment on start
#   COMFYUI_DEV_MODEL_HUB_PATH       shared model directory; defaults to the installed mount
#   COMFYUI_DEV_DATA_DIR             dev data directory, default $COMFYUI_DEV_STAGE_DIR/data
#   VOS_TRAEFIK_CONTAINER(traefik)   VOS_HOST_GW_IP(172.17.0.1)
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

COMPOSE_FILE="docker/vos-dev/docker-compose.yml"
ROUTE_TMPL="docker/vos-dev/traefik.dev.yml.tmpl"
APP_ID="com.ictrek.comfyui"
SERVICE="comfyui-dev"
PORT="${VOS_DEV_PORT:-38093}"
HOST_GW="${VOS_HOST_GW_IP:-172.17.0.1}"
GW_PORT="${VOS_API_GW_PORT_INTERNAL:-1182}"
TRAEFIK_CONTAINER="${VOS_TRAEFIK_CONTAINER:-traefik}"
ROUTE_FILE="/etc/traefik/dynamic-conf/${APP_ID}.dev.yml"
STAGE_DIR="${COMFYUI_DEV_STAGE_DIR:-$HOME/dev/build/ComfyUI/vos-dev}"
ENV_FILE="$STAGE_DIR/compose.env"
HEALTH_TIMEOUT="${COMFYUI_DEV_HEALTH_TIMEOUT:-180}"

# Overrides are read once, so prepare() only fills what the caller left empty.
IMAGE_OVERRIDE="${COMFYUI_DEV_BASE_IMAGE:-}"
RUNTIME_OVERRIDE="${COMFYUI_DEV_RUNTIME:-}"
DEVICE_OVERRIDE="${COMFYUI_DEV_DEVICE:-}"
HUB_OVERRIDE="${COMFYUI_DEV_MODEL_HUB_PATH:-}"

# Placeholders so down, logs and status work without reading the installed app.
export COMFYUI_DEV_BASE_IMAGE="${IMAGE_OVERRIDE:-unresolved}"
export COMFYUI_DEV_RUNTIME="${RUNTIME_OVERRIDE:-runc}"
export COMFYUI_DEV_DEVICE="${DEVICE_OVERRIDE:-cpu}"
export COMFYUI_DEV_SOURCE="$ROOT_DIR"
export COMFYUI_DEV_DATA="${COMFYUI_DEV_DATA_DIR:-$STAGE_DIR/data}"
export COMFYUI_DEV_VENDOR="$STAGE_DIR/vendor"
export COMFYUI_DEV_MODEL_HUB="${HUB_OVERRIDE:-$STAGE_DIR/modelhub}"
export COMFYUI_DEV_ENV_FILE="$ENV_FILE"
export VOS_DEV_PORT="$PORT"

# compose reads the environment file even for down, logs and status.
mkdir -p "$STAGE_DIR" 2>/dev/null || true
[ -f "$ENV_FILE" ] || touch "$ENV_FILE" 2>/dev/null || true

# Settings the dev container owns. Everything else the installed app is given is a
# platform or sibling-service address, and the debugged instance needs the same ones.
ENV_DENY='^(PATH|HOME|HOSTNAME|PWD|OLDPWD|SHLVL|TERM|_|LANG|LC_ALL|GPG_KEY|PYTHON_VERSION|PYTHON_SHA256|PYTHON_GET_PIP_URL|PYTHON_GET_PIP_SHA256|PYTHONUNBUFFERED|PYTHONDONTWRITEBYTECODE|PIP_BREAK_SYSTEM_PACKAGES|PIP_CONFIG_FILE|HF_HUB_OFFLINE|TRANSFORMERS_OFFLINE|HF_HUB_DISABLE_TELEMETRY|NVIDIA_VISIBLE_DEVICES|NVIDIA_DRIVER_CAPABILITIES|VOS_APP_STORAGE_PATH|COMFYUI_DATA_DIR|COMFYUI_MODEL_HUB_MOUNT|COMFYUI_MODEL_INDEX)='

log() { printf '\033[36m[vos-dev]\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[vos-dev] %s\033[0m\n' "$*" >&2; }
die() { printf '\033[31m[vos-dev][ERROR]\033[0m %s\n' "$*" >&2; exit 1; }

compose() { docker compose -p comfyui-vos-dev -f "$COMPOSE_FILE" "$@"; }

# The installed application. The dev container is named comfyui-vos-dev, so it never
# matches the platform's vos-app-<app id>-... naming.
app_container() {
  docker ps --format '{{.Names}}' | grep -E '^vos-app-com-ictrek-comfyui-' | head -n1
}

app_env() {
  docker inspect "$1" --format '{{range .Config.Env}}{{println .}}{{end}}' | sed -n "s/^$2=//p"
}

app_mount() {
  docker inspect "$1" --format "{{range .Mounts}}{{if eq .Destination \"$2\"}}{{.Source}}{{end}}{{end}}"
}

ensure_port_free() {
  if (exec 3<>"/dev/tcp/127.0.0.1/$PORT") 2>/dev/null; then
    die "port $PORT is already in use; stop whatever holds it or run with VOS_DEV_PORT=<free port>"
  fi
}

# The image ships the GGUF nodes under /opt/comfyui/vos/vendor, and the source mount
# covers that path. Copy them to the stage directory once and mount them back.
prepare_vendor() {
  local target="$COMFYUI_DEV_VENDOR/ComfyUI-GGUF"
  [ -f "$target/nodes.py" ] && return 0
  mkdir -p "$target"
  log "copying the bundled GGUF nodes out of $1"
  if docker cp "$1:/opt/comfyui/vos/vendor/ComfyUI-GGUF/." "$target" >/dev/null 2>&1; then
    return 0
  fi
  warn "cannot copy the bundled GGUF nodes: GGUF workflows stay broken in the dev editor"
}

# New dependencies need a rebuilt image; the mounted source cannot install them.
compare_requirements() {
  diff -q <(docker exec "$1" cat /opt/comfyui/requirements.txt) requirements.txt >/dev/null 2>&1 && return 0
  warn "requirements.txt differs from the installed image: reinstall the app or rebuild the image for new dependencies"
}

prepare() {
  mkdir -p "$COMFYUI_DEV_DATA" "$COMFYUI_DEV_VENDOR" "$COMFYUI_DEV_MODEL_HUB" "$ROOT_DIR/vos/vendor" 2>/dev/null \
    || die "cannot create $STAGE_DIR; run with COMFYUI_DEV_STAGE_DIR=<writable directory>"
  local installed
  installed="$(app_container || true)"
  [ -n "$installed" ] || die "no installed ComfyUI container found; install the app in VOS first"

  if [ -z "$IMAGE_OVERRIDE" ]; then
    COMFYUI_DEV_BASE_IMAGE="$(docker inspect "$installed" --format '{{.Config.Image}}')"
    export COMFYUI_DEV_BASE_IMAGE
  fi
  if [ -z "$RUNTIME_OVERRIDE" ]; then
    COMFYUI_DEV_RUNTIME="$(docker inspect "$installed" --format '{{.HostConfig.Runtime}}')"
    [ "$COMFYUI_DEV_RUNTIME" = "nvidia" ] || COMFYUI_DEV_RUNTIME="runc"
    export COMFYUI_DEV_RUNTIME
  fi
  if [ -z "$DEVICE_OVERRIDE" ]; then
    COMFYUI_DEV_DEVICE="$(app_env "$installed" COMFYUI_DEVICE)"
    COMFYUI_DEV_DEVICE="${COMFYUI_DEV_DEVICE:-cpu}"
    export COMFYUI_DEV_DEVICE
  fi
  if [ -z "$HUB_OVERRIDE" ]; then
    local hub
    hub="$(app_mount "$installed" /modelhub)"
    if [ -n "$hub" ]; then
      COMFYUI_DEV_MODEL_HUB="$hub"
      export COMFYUI_DEV_MODEL_HUB
    else
      warn "the installed app exposes no /modelhub mount: the dev editor sees no shared weights"
    fi
  fi

  if [ ! -s "$ENV_FILE" ] || [ "${COMFYUI_DEV_REFRESH_ENV:-0}" = 1 ]; then
    log "copying the platform environment of $installed"
    {
      printf '# Written by docker/vos-dev/vos-dev.sh from %s\n' "$installed"
      docker inspect "$installed" --format '{{range .Config.Env}}{{println .}}{{end}}' | grep -Ev "$ENV_DENY" || true
    } > "$ENV_FILE"
  fi

  prepare_vendor "$installed"
  compare_requirements "$installed"
}

route_on() {
  if ! docker ps --format '{{.Names}}' | grep -qx "$TRAEFIK_CONTAINER"; then
    warn "no Traefik container $TRAEFIK_CONTAINER: no route written, open http://${HOST_GW}:${PORT}/health instead"
    return 0
  fi
  sed -e "s|__APP_ID__|$APP_ID|g" -e "s|__HOST__|$HOST_GW|g" -e "s|__PORT__|$PORT|g" "$ROUTE_TMPL" \
    | docker exec -i "$TRAEFIK_CONTAINER" sh -c "cat > '$ROUTE_FILE'"
  log "route enabled: /app/${APP_ID} -> http://${HOST_GW}:${PORT} (removed on exit)"
}

route_off() {
  docker ps --format '{{.Names}}' | grep -qx "$TRAEFIK_CONTAINER" || return 0
  docker exec "$TRAEFIK_CONTAINER" rm -f "$ROUTE_FILE" 2>/dev/null || true
  log "route removed; the platform serves the installed app again"
}

wait_healthy() {
  local waited=0
  while [ "$waited" -lt "$HEALTH_TIMEOUT" ]; do
    if curl -fsS -m 3 "http://${HOST_GW}:${PORT}/health" >/dev/null 2>&1; then
      [ "$waited" -gt 0 ] && echo
      return 0
    fi
    printf '\r\033[36m[vos-dev]\033[0m waiting for the gateway on :%s ... %ss/%ss' "$PORT" "$waited" "$HEALTH_TIMEOUT"
    sleep 3
    waited=$((waited + 3))
  done
  echo
  return 1
}

cleanup() {
  local rc=$?
  printf '\n'
  log "leaving live debugging"
  set +e
  route_off
  compose down
  return "$rc"
}

cmd_up() {
  command -v docker >/dev/null || die "docker is required"
  command -v curl >/dev/null || die "curl is required"
  [ -f "$ROUTE_TMPL" ] || die "missing route template $ROUTE_TMPL"
  docker network inspect vos_default >/dev/null 2>&1 \
    || die "docker network vos_default is missing; start VOS first"

  ensure_port_free
  prepare

  log "starting the dev container: image $COMFYUI_DEV_BASE_IMAGE, device $COMFYUI_DEV_DEVICE, gateway :${PORT}"
  compose up -d

  if ! wait_healthy; then
    warn "the gateway did not answer in ${HEALTH_TIMEOUT}s; the route stays off, so the installed app keeps serving"
    warn "read the log with make vos-dev-logs, then run make vos-dev again"
    exit 1
  fi
  log "gateway healthy"

  route_on
  log "ready: open ComfyUI from the platform sidebar, or reload its iframe"
  log "  the first editor open starts a worker; wait for 'To see the GUI go to' in this log (about two minutes cold)"
  log "  other apps reach this container as comfyui-dev:8188"
  log "  Ctrl-C removes the route and stops the container"
  printf '\n'

  trap cleanup EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
  compose logs -f "$SERVICE"
}

cmd_down() {
  set +e
  route_off
  compose down
  log "dev container stopped"
}

cmd_status() {
  echo "-- dev container (:${PORT}) --"
  compose ps "$SERVICE" --format '  {{.Name}}  {{.Status}}' 2>/dev/null || echo "  not running"
  echo "-- gateway --"
  if curl -fsS -m 3 "http://${HOST_GW}:${PORT}/health"; then
    echo
  else
    echo "  unreachable on :${PORT}"
  fi
  echo "-- temporary route --"
  if docker ps --format '{{.Names}}' | grep -qx "$TRAEFIK_CONTAINER"; then
    if docker exec "$TRAEFIK_CONTAINER" test -f "$ROUTE_FILE"; then
      echo "  installed: $ROUTE_FILE"
    else
      echo "  absent; the platform serves the installed app"
    fi
  else
    echo "  no Traefik container $TRAEFIK_CONTAINER"
  fi
  echo "-- platform link --"
  local headers
  headers="$(curl -sS -m 5 -D- -o /dev/null "http://${HOST_GW}:${GW_PORT}/app/${APP_ID}/" 2>/dev/null || true)"
  if [ -z "$headers" ]; then
    echo "  unreachable on ${HOST_GW}:${GW_PORT}"
  elif printf '%s' "$headers" | grep -qi '^x-comfyui-dev:'; then
    echo "  dev container (X-ComfyUI-Dev marker present)"
  else
    echo "  installed app (no marker header)"
  fi
}

cmd_logs() { compose logs -f "$SERVICE"; }

cmd_shell() { compose exec "$SERVICE" bash; }

cmd_reload() {
  compose restart "$SERVICE"
  log "gateway restarted; sessions are in memory, so the browser signs in again"
}

case "${1:-up}" in
  up) cmd_up ;;
  down) cmd_down ;;
  logs) cmd_logs ;;
  status) cmd_status ;;
  shell) cmd_shell ;;
  reload) cmd_reload ;;
  route-on) route_on ;;
  route-off) route_off ;;
  *) die "unknown command ${1:-} (up|down|logs|status|shell|reload|route-on|route-off)" ;;
esac

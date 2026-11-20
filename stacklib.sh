# Shared logic for stack launcher scripts (installed to /opt/podman-panel/stacklib.sh).
#
# A stack script sets POD and IMAGES, defines create() (podman pod create + podman run --pod "$POD" …),
# optionally build() for custom images, then ends with:
#
#   source /opt/podman-panel/stacklib.sh
#   stack_main "$@"
#
# Commands:
#   up        create the stack from the script and hand it to systemd (start on boot)   [default]
#   update    pull images (and build()), then the same as up
#   down      stop the stack and disable its unit (volumes and data are kept)
#   status    show the pod, its containers and the unit
#
# Systemd owns the running stack: after creating it, the script regenerates the unit from it
# (podman generate systemd --new) and restarts it through that unit. Script and unit never drift apart,
# and nothing runs "outside systemd".
#
# Secrets go in ENV_FILE (default: next to the script, <script>.env, mode 600), passed to containers
# with --env-file "$ENV_FILE". Never put them in the script.

set -euo pipefail

: "${POD:?set POD before sourcing stacklib.sh}"
STACK_SCRIPT=$(readlink -f "${BASH_SOURCE[1]}")
ENV_FILE=${ENV_FILE:-${STACK_SCRIPT%.sh}.env}
UNIT="pod-$POD.service"
if [[ $EUID -eq 0 ]]; then
  SYSTEMCTL=(systemctl); UNIT_DIR=/etc/systemd/system
else
  SYSTEMCTL=(systemctl --user); UNIT_DIR="$HOME/.config/systemd/user"
  export XDG_RUNTIME_DIR=${XDG_RUNTIME_DIR:-/run/user/$EUID}
fi

say() { printf '==> %s\n' "$*"; }

stack_ensure_env() {
  [[ -f "$ENV_FILE" ]] && return
  (umask 077; : > "$ENV_FILE")
  say "created empty $ENV_FILE (mode 600) for secrets"
}

stack_pull() {
  local img
  for img in "${IMAGES[@]}"; do
    [[ $img == localhost/* ]] && continue
    say "pull $img"
    podman pull "$img"
  done
}

stack_remove() {
  if podman pod exists "$POD"; then
    say "remove pod $POD"
    podman pod stop --ignore -t 30 "$POD" >/dev/null
    podman pod rm -f --ignore "$POD" >/dev/null
  fi
}

stack_up() {
  stack_ensure_env
  "${SYSTEMCTL[@]}" stop "$UNIT" 2>/dev/null || true
  stack_remove
  say "create pod $POD"
  create
  # Generate the unit from what create() made (by pod ID: a container may share the pod's name),
  # then let systemd recreate and own it.
  local id
  id=$(podman pod inspect --format '{{.Id}}' "$POD")
  mkdir -p "$UNIT_DIR"
  (cd "$UNIT_DIR" && podman generate systemd --new --files --name "$id" >/dev/null 2>&1)
  stack_remove
  "${SYSTEMCTL[@]}" daemon-reload
  "${SYSTEMCTL[@]}" enable "$UNIT" >/dev/null 2>&1
  say "start $UNIT"
  "${SYSTEMCTL[@]}" start "$UNIT"
  stack_status
}

stack_down() {
  "${SYSTEMCTL[@]}" disable --now "$UNIT" 2>/dev/null || true
  stack_remove
  "${SYSTEMCTL[@]}" reset-failed "$UNIT" 2>/dev/null || true
  say "$POD stopped; start on boot disabled; volumes and data kept"
}

stack_status() {
  podman ps -a --pod --filter "pod=$POD" --format 'table {{.Names}}\t{{.Status}}\t{{.Image}}'
  printf '%s: %s, %s\n' "$UNIT" "$("${SYSTEMCTL[@]}" is-active "$UNIT" 2>/dev/null || true)" \
    "$("${SYSTEMCTL[@]}" is-enabled "$UNIT" 2>/dev/null || true)"
}

stack_main() {
  case "${1:-up}" in
    up) stack_up ;;
    update)
      stack_pull
      if declare -F build >/dev/null; then say "build"; build; fi
      stack_up ;;
    down) stack_down ;;
    status) stack_status ;;
    *) echo "usage: $(basename "$STACK_SCRIPT") [up|update|down|status]"; exit 2 ;;
  esac
}

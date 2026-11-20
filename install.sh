#!/usr/bin/env bash
# Install or upgrade Podman Panel: the root agent and the sandboxed web front end, both systemd services.
#   sudo ./install.sh                                  first install / upgrade from this checkout
#   sudo PANEL_LISTEN=10.0.0.5:9191 ./install.sh       listen somewhere other than 127.0.0.1:9191
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "Run with sudo"; exit 1; }
SRC=$(cd "$(dirname "$0")" && pwd -P)
DEST=/opt/podman-panel
CFG=/etc/podman-panel/config.json

# The agent runs this code as root, so it lives in a root-owned copy (with .git, for self-update).
if [[ "$SRC" != "$DEST" ]]; then
  echo "Installing $SRC to $DEST"
  rm -rf "$DEST.new"
  cp -a "$SRC" "$DEST.new"
  rm -rf "$DEST"
  mv "$DEST.new" "$DEST"
fi
chown -R root:root "$DEST"
chmod -R go-w "$DEST"
chmod -R a+rX "$DEST"
cd "$DEST"

# Unprivileged user for the web front end; the agent's socket is group-accessible to it only.
if ! getent passwd podman-panel >/dev/null; then
  useradd --system --user-group --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin podman-panel
fi

python3 -m podman_panel setup --config "$CFG"
LISTEN=$(python3 - "$CFG" "${PANEL_LISTEN:-}" <<'PY'
import json, sys
path, new = sys.argv[1], sys.argv[2]
with open(path) as f:
    cfg = json.load(f)
if new:
    cfg["listen"] = new
    with open(path, "w") as f:
        json.dump(cfg, f, indent=2)
print(cfg.get("listen", "127.0.0.1:9191"))
PY
)
BIND=${LISTEN%:*}
PORT=${LISTEN##*:}

# Earlier versions: one root service, or the web part as a container.
for old in podman-panel.service; do
  if [[ -f /etc/systemd/system/$old ]]; then
    systemctl disable --now "$old" || true
    rm -f "/etc/systemd/system/$old"
  fi
done
if [[ -f /etc/containers/systemd/podman-panel.container ]]; then
  systemctl stop podman-panel.service || true
  rm -f /etc/containers/systemd/podman-panel.container
  podman rmi -f localhost/podman-panel:latest >/dev/null 2>&1 || true
fi

install -m 644 podman-panel-agent.service /etc/systemd/system/podman-panel-agent.service
sed -e "s|@BIND@|$BIND|" -e "s|@PORT@|$PORT|" podman-panel-web.service \
  > /etc/systemd/system/podman-panel-web.service
chmod 644 /etc/systemd/system/podman-panel-web.service
systemctl daemon-reload
systemctl enable podman-panel-agent.service podman-panel-web.service >/dev/null
echo "Restarting"
systemctl restart podman-panel-agent.service
systemctl restart podman-panel-web.service
sleep 2
systemctl --no-pager --lines=3 status podman-panel-agent.service podman-panel-web.service || true
echo
echo "Podman Panel listens on $LISTEN (plain HTTP). Point your reverse proxy (TLS) at it."

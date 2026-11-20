#!/usr/bin/env bash
# Install or upgrade Podman Panel: the root agent and the sandboxed web front end, both systemd services.
#   sudo ./install.sh                                  first install / upgrade from this checkout
#   sudo PANEL_LISTEN=192.168.1.10:9191 ./install.sh   listen on the LAN address (proxy in a container)
#   sudo PANEL_ALLOW="localhost 10.88.0.0/16" ./install.sh   addresses allowed to connect (saved in the config)
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
# listen: where the web service listens. allow_from: the only source addresses it accepts
# (enforced by systemd's per-service IP firewall). Defaults: loopback plus the listen address itself,
# which is where a rootless (slirp4netns) nginx container on this host connects from.
read -r LISTEN ALLOW < <(python3 - "$CFG" "${PANEL_LISTEN:-}" "${PANEL_ALLOW:-}" <<'PY'
import json, sys
path, listen, allow = sys.argv[1:4]
with open(path) as f:
    cfg = json.load(f)
if listen:
    cfg["listen"] = listen
if allow:
    cfg["allow_from"] = allow.split()
if listen or allow:
    with open(path, "w") as f:
        json.dump(cfg, f, indent=2)
listen = cfg.get("listen", "127.0.0.1:9191")
ip = listen.rsplit(":", 1)[0].strip("[]")
default = ["localhost"] + ([] if ip.startswith("127.") or ip in ("::1", "localhost") else [ip])
print(listen, " ".join(cfg.get("allow_from", default)))
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
sed -e "s|@BIND@|$BIND|" -e "s|@PORT@|$PORT|" -e "s|@ALLOW@|$ALLOW|" podman-panel-web.service \
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
echo "Podman Panel listens on $LISTEN (plain HTTP), accepting connections only from: $ALLOW"
echo "Point your reverse proxy (TLS) at it."

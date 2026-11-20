"""python3 -m podman_panel <command>

  agent                 root service: podman/systemd access, login, jobs (unix socket)
  web                   UI front end for the container (talks to the agent's socket)
  setup                 first-time config: password, TOTP, host names (asks only for what is missing)
  passwd                change the password
  totp                  issue a new TOTP secret (the old one stops working)
  hosts NAME [NAME...]  host names the panel is reached by, e.g. panel.home.lan
"""
import argparse
import getpass
import json
import os
import sys

from . import auth

DEFAULT_CONFIG = "/etc/podman-panel/config.json"


def load(path):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save(path, cfg):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = path + ".new"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, path)
    print(f"Saved {path}. Restart the agent to apply: systemctl restart podman-panel-agent")


def ask_password(cfg):
    pw = getpass.getpass("Panel password (min. 12 characters): ")
    if len(pw) < 12 or pw != getpass.getpass("Repeat: "):
        raise SystemExit("Passwords differ or are shorter than 12 characters.")
    cfg["password_hash"] = auth.hash_password(pw)


def new_totp(cfg):
    cfg["totp_secret"] = auth.new_totp_secret()
    label = os.uname().nodename
    print("\nAdd this to your authenticator app (Aegis, 2FAS, Google Authenticator, 1Password …):")
    print(f"  secret: {cfg['totp_secret']}")
    print(f"  or URI: {auth.totp_uri(cfg['totp_secret'], label)}\n")


def ask_hosts(cfg):
    names = input("Host name(s) the panel is reached by through your proxy, space separated "
                  "(e.g. panel.home.lan): ").split()
    cfg["allowed_hosts"] = [n.lower() for n in names]


def main():
    ap = argparse.ArgumentParser(prog="podman_panel", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["agent", "web", "setup", "passwd", "totp", "hosts"])
    ap.add_argument("names", nargs="*")
    ap.add_argument("--config", default=os.environ.get("PANEL_CONFIG", DEFAULT_CONFIG))
    args = ap.parse_args()

    if args.command == "web":
        from . import web
        web.serve(os.environ.get("PANEL_SOCKET", "/run/podman-panel/agent.sock"),
                  os.environ.get("PANEL_BIND", "127.0.0.1"), int(os.environ.get("PANEL_PORT", "9191")))
        return
    cfg = load(args.config)
    if args.command == "agent":
        from . import agent
        agent.serve(cfg)
    elif args.command == "setup":
        changed = False
        if not cfg.get("password_hash"):
            ask_password(cfg)
            changed = True
        if not cfg.get("totp_secret"):
            new_totp(cfg)
            changed = True
        if "allowed_hosts" not in cfg:
            ask_hosts(cfg)
            changed = True
        if changed:
            save(args.config, cfg)
    elif args.command == "passwd":
        ask_password(cfg)
        save(args.config, cfg)
    elif args.command == "totp":
        new_totp(cfg)
        save(args.config, cfg)
    elif args.command == "hosts":
        if not args.names:
            sys.exit("give at least one host name")
        cfg["allowed_hosts"] = [n.lower() for n in args.names]
        save(args.config, cfg)


if __name__ == "__main__":
    main()

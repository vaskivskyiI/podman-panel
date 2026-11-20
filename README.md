# Podman Panel

A web UI for the podman pods and containers of every user on one server. Python standard library only.

## What it does

- **Stacks.** Every pod and standalone container of root and of each lingering user: state, health,
  ports, CPU and memory, details (environment values hidden) and the systemd unit. Start, stop, restart.
- **Logs.** Last 200 / 1000 / 5000 lines, live follow, line filter, download.
- **Start on boot.** A toggle per stack. With no unit yet it runs `podman generate systemd --new` first.
- **Updates.**
  - *Public images:* the registry digest of the tag (for this host's architecture) is compared with the
    local image, using anonymous HEAD requests. Every 6 hours and on demand.
  - *Custom images:* the git checkout the image is built from is fetched; new upstream commits, or a
    commit newer than the image, mean an update.
  - **Update** runs your launcher script with `update` (`~/podman/<x>.sh` or `~/<project>/deploy/*.sh`,
    matched by `POD_NAME=`/`POD=`); without one it pulls and recreates through the systemd unit.
- **Rollback.** Before every update the images that run now are tagged `<image>:<tag>-pp-rollback`.
  "Roll back to previous image" puts that tag back and recreates the stack through its unit.
- **Images.** Prune dangling or all unused images per account.
- **Insights.** Disk pressure, stacks that won't start after reboot, stacks running outside their unit,
  orphaned units, long-stopped stacks, databases published on all interfaces or on `:latest`, missing
  healthchecks, `--privileged`, very old images, lingering off, Quadlet migration.
- **Jobs and activity log.** Every change runs as a cancellable job with live output. Every sign-in,
  failed sign-in and change is written to `/var/log/podman-panel/audit.log` and shown in the UI.
- **Self-update.** "Update panel" on the Overview pulls the panel's git checkout and reinstalls it.

## Design

```
browser ─TLS─> your reverse proxy ─HTTP─> podman-panel-web ─unix socket─> podman-panel-agent ─runuser─> podman / systemctl --user
                                          (user podman-panel,             (root; holds login,
                                           systemd sandbox, no secrets)    validates everything)
```

Both parts are systemd services on the host:

- **`podman-panel-agent.service`** (root). Managing other users' rootless podman and `systemctl --user`
  needs root. It listens only on `/run/podman-panel/agent.sock` (mode 660, group `podman-panel`) and
  also checks the caller's uid with `SO_PEERCRED`. It owns the password hash, the TOTP secret and the
  sessions, validates every request, and runs only allow-listed operations on stacks, units and scripts
  it discovered itself. There is no free-form command input anywhere.
- **`podman-panel-web.service`** (system user `podman-panel`). The only part reachable over the
  network: it serves the UI and forwards `/api/*` to the socket. It has no capabilities, cannot gain
  privileges, sees a read-only system, no home directories, no devices and no other processes, cannot
  read the config or audit log, and is limited to a filtered set of system calls, 256 MB and 64 tasks.
  It holds no secrets and can do nothing without a session the agent issued.

## Security

The panel can do whatever any managed user can do with podman. These are the protections, for the case
of a compromised device on the LAN:

- **Login** needs the password (PBKDF2-SHA256, 600k iterations) and a TOTP code. Codes cannot be
  replayed. After 5 failures, every further attempt waits exponentially longer (up to 5 minutes).
- **Sessions** are random 256-bit tokens. The agent stores only their hash. They end after 1 hour idle
  or 12 hours, and on agent restart. "Sign out all sessions" is in the Jobs tab.
- **Cookie** `__Host-pp`: `Secure`, `HttpOnly`, `SameSite=Strict`. Browsers only send it over HTTPS (or
  to `localhost`), so the plain-HTTP port cannot be used to sign in from the LAN.
- **CSRF:** every POST needs an `X-Panel` header (forces a CORS preflight, which is never granted) and
  a matching `Origin`.
- **DNS rebinding:** requests whose `Host` is not in `allowed_hosts` (or `localhost`) are refused.
- **Headers:** strict CSP (no inline script, no framing), `nosniff`, `no-referrer`, COOP/CORP.
- **Network:** listens on `127.0.0.1:9191` by default, so only a reverse proxy on this host reaches it.
- **Self-update** runs the installer only if the checkout is owned by root and not group/world writable.

Not covered: anyone with a root shell, or with a shell as a managed user, can bypass the panel anyway.
A compromised web service could read the session cookie and password of someone who signs in while
it is compromised.

## Install

```bash
git clone <this repo> podman-panel      # or copy the folder
cd podman-panel && sudo ./install.sh
```

The first run asks for a password (12+ characters), prints a TOTP secret for your authenticator app, and
asks for the host name(s) your proxy serves it as (e.g. `panel.home.lan`). It copies the code to
`/opt/podman-panel` (root-owned; this is where self-update pulls), creates the `podman-panel` system
user, installs both units and starts them.

Then point the reverse proxy at `127.0.0.1:9191`.

### nginx

```nginx
server {
    listen 443 ssl;
    http2 on;
    server_name panel.home.lan;                  # must be in allowed_hosts

    ssl_certificate     /etc/nginx/certs/panel.home.lan.crt;
    ssl_certificate_key /etc/nginx/certs/panel.home.lan.key;
    ssl_protocols TLSv1.2 TLSv1.3;

    # Only your own networks; adjust to your LAN / VPN ranges.
    allow 192.168.1.0/24;
    deny  all;

    add_header Strict-Transport-Security "max-age=31536000" always;
    client_max_body_size 128k;

    location / {
        proxy_pass http://127.0.0.1:9191;
        proxy_set_header Host $http_host;        # original Host incl. port: checked against allowed_hosts and Origin
        proxy_set_header X-Forwarded-For $remote_addr;   # replaces any client-supplied value; shown in the audit log
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 150s;                 # some actions (logs, stats) take a while
    }
}

server {
    listen 80;
    server_name panel.home.lan;
    return 301 https://$host$request_uri;
}
```

Notes:
- Use `$http_host`, not `$host`, so a non-443 port stays in the `Host` header and matches the
  browser's `Origin`. Otherwise every change is refused as cross-site.
- Set `X-Forwarded-For` to `$remote_addr` instead of appending with `$proxy_add_x_forwarded_for`, so a
  client cannot put fake addresses in the audit log.
- The certificate can come from your internal CA, `mkcert`, or Let's Encrypt with a DNS challenge.

### Caddy

```
panel.home.lan {
    tls internal
    @lan remote_ip 192.168.1.0/24
    handle @lan {
        reverse_proxy 127.0.0.1:9191
    }
    respond 403
}
```

If the proxy runs in a rootless container that cannot reach the host's loopback, listen on another
address and firewall it: `sudo PANEL_LISTEN=10.0.0.5:9191 ./install.sh`.

For self-update, the git remote must be readable by root without your SSH key (an `https://` URL).

## Configuration

`/etc/podman-panel/config.json` (root, mode 600). After changes, `sudo systemctl restart podman-panel-agent`.

| Key | Default | |
|---|---|---|
| `allowed_hosts` | asked | host names the panel is reached by |
| `users` | root + all lingering users | accounts to manage |
| `listen` | `127.0.0.1:9191` | address of the web service (rerun `install.sh` after changing) |

```bash
cd /opt/podman-panel
sudo python3 -m podman_panel passwd     # new password
sudo python3 -m podman_panel totp       # new TOTP secret
sudo python3 -m podman_panel hosts panel.home.lan panel.example.org
```

"""The privileged half: a root service that owns login, sessions, podman/systemd access and jobs.

It listens only on a unix socket that the web service's user may connect to (checked with
SO_PEERCRED as well as file permissions). Every request except login needs a session token that this
process issued, so the web service holds no secrets and can do nothing without a signed-in user.
Every input is validated here; the web service is treated as untrusted.
"""
import concurrent.futures
import http.server
import json
import os
import pwd
import socket
import socketserver
import stat
import struct
import threading
import time
import urllib.parse

from . import host, insights, registry
from .auth import Auth, AuthError
from .jobs import JobManager

SNAP_TTL = 5
UPDATE_INTERVAL = 6 * 3600
DEFAULTS = {
    "socket": "/run/podman-panel/agent.sock",
    "web_user": "podman-panel",
    "audit_log": "/var/log/podman-panel/audit.log",
    "source_dir": "/opt/podman-panel",
    "allowed_hosts": [],
}


class ApiError(Exception):
    def __init__(self, msg, code=400):
        super().__init__(msg)
        self.code = code


class Panel:
    def __init__(self, config):
        self.config = {**DEFAULTS, **config}
        self.accounts = {a.name: a for a in host.managed_accounts(self.config.get("users"))}
        self.is_root = os.geteuid() == 0
        self.auth = Auth(self.config)
        self.jobs = JobManager()
        self.snap_lock = threading.Lock()
        self.snaps, self.snap_time = [], 0
        self.updates = {}  # account -> {"checked": ts, "images": {ref: {...}}, "sources": {path: {...}}}
        self.panel_source = None  # git state of the panel's own checkout, from the last update check
        self.update_state = {"running": False, "started": None, "finished": None}
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=8)
        self.audit_lock = threading.Lock()

    # -- audit trail: every login and every change, to a file and the journal
    def audit(self, client, event, **fields):
        entry = {"time": time.time(), "client": client, "event": event, **fields}
        line = json.dumps(entry, default=str)
        print("audit " + line, flush=True)
        path = self.config["audit_log"]
        try:
            with self.audit_lock:
                os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
                fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
                with os.fdopen(fd, "a") as f:
                    f.write(line + "\n")
        except OSError as e:
            print(f"audit log not writable: {e}", flush=True)

    def audit_tail(self, n=200):
        try:
            with open(self.config["audit_log"], "rb") as f:
                f.seek(0, os.SEEK_END)
                f.seek(max(0, f.tell() - 200_000))
                lines = f.read().decode(errors="replace").splitlines()[-n:]
        except OSError:
            return []
        out = []
        for line in reversed(lines):
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
        return out

    # -- snapshots
    def snapshots(self, force=False):
        with self.snap_lock:
            if force or time.time() - self.snap_time > SNAP_TTL:
                self.snaps = list(self.pool.map(host.snapshot, self.accounts.values()))
                self.snap_time = time.time()
            return self.snaps

    def invalidate(self, *_):
        self.snap_time = 0

    def account(self, name):
        acc = self.accounts.get(name) if isinstance(name, str) else None
        if not acc:
            raise ApiError("unknown account", 404)
        return acc

    def stack(self, acc, key):
        snap = next(s for s in self.snapshots(force=True) if s["account"] == acc.name)
        st = next((s for s in snap["stacks"] if s["key"] == key), None)
        if not st:
            raise ApiError(f"{key} not found", 404)
        return snap, st

    # -- update checks
    def check_updates(self):
        if self.update_state["running"]:
            return
        self.update_state.update(running=True, started=time.time())
        threading.Thread(target=self._check_updates, daemon=True).start()

    def _check_updates(self):
        try:
            for s in self.snapshots(force=True):
                acc = self.accounts[s["account"]]
                digests = {im["id"]: {d.split("@", 1)[-1] for d in im["digests"]} for im in s["images"]}
                todo, images = {}, {}
                for st in s["stacks"]:
                    for c in st["containers"]:
                        ref = c["image"]
                        if not ref or ref in images or ref in todo:
                            continue
                        parsed = registry.parse_ref(ref)
                        if ref.startswith("localhost/"):
                            images[ref] = {"status": "custom"}
                        elif not parsed:
                            images[ref] = {"status": "pinned"}
                        else:
                            todo[ref] = (parsed, digests.get(c["image_id"], set()))

                def lookup(item):
                    ref, (parsed, local) = item
                    try:
                        return ref, registry.check(*parsed, local)
                    except Exception as e:  # network/registry errors are reported per image
                        return ref, {"status": "unknown", "error": str(e)[:200]}

                images.update(self.pool.map(lookup, todo.items()))
                dirs = {d for st in s["stacks"] for d in st["sources"]}
                sources = dict(zip(dirs, self.pool.map(lambda d: host.source_status(acc, d, True), dirs)))
                self.updates[s["account"]] = {"checked": time.time(), "images": images, "sources": sources}
            src = self.config["source_dir"]
            if os.path.isdir(os.path.join(src, ".git")):
                me = host.Account(pwd.getpwuid(os.geteuid()).pw_name)
                self.panel_source = host.source_status(me, src, True)
        finally:
            self.update_state.update(running=False, finished=time.time())

    def update_loop(self):
        time.sleep(20)
        while True:
            self.check_updates()
            time.sleep(UPDATE_INTERVAL)

    # -- state
    def state(self, force=False):
        snaps = self.snapshots(force)
        h = host.host_info()
        return {"host": h, "accounts": snaps, "updates": self.updates, "update_state": self.update_state,
                "insights": insights.build(h, snaps, self.updates, self.is_root),
                "jobs": self.jobs.list()[:30], "is_root": self.is_root,
                "panel": {"source_dir": self.config["source_dir"], "source": self.panel_source}}

    # -- actions
    def action(self, body, client):
        if not isinstance(body, dict):
            raise ApiError("bad request")
        op = body.get("op")
        if op == "self_update":
            me = host.Account(pwd.getpwuid(os.geteuid()).pw_name)
            result = self._job(me, "panel: update", "panel", self._self_update_steps())
            self.audit(client, "action", op=op, account=me.name, job=result["job"])
            return result
        acc = self.account(body.get("account"))
        result = self._action(op, acc, body)
        self.audit(client, "action", op=op, account=acc.name,
                   **{k: body[k] for k in ("stack", "unit", "script", "arg") if isinstance(body.get(k), str)},
                   job=result["job"])
        return result

    def _action(self, op, acc, body):
        sc = acc.systemctl_argv
        if op in ("prune_dangling", "prune_unused"):
            argv = ["podman", "image", "prune", "-f"] + (["-a"] if op == "prune_unused" else [])
            return self._job(acc, f"{acc.name}: prune {'unused' if op == 'prune_unused' else 'dangling'} images",
                             "images", [(argv, None)])
        if op == "enable_linger":
            if not self.is_root:
                raise ApiError("needs the agent to run as root")
            return self._job(acc, f"{acc.name}: enable lingering", "linger",
                             [(["loginctl", "enable-linger", acc.name], "/")])
        if op == "remove_orphan_unit":
            unit = body.get("unit")
            snap = next(s for s in self.snapshots(force=True) if s["account"] == acc.name)
            orphan = next((o for o in snap.get("orphan_units", []) if o["name"] == unit), None)
            if not orphan:
                raise ApiError("not an orphaned unit")
            units = [unit] + [u for u in host.wanted_units(os.path.join(acc.unit_dir, unit))
                              if u in snap["unit_files"]]
            paths = [os.path.join(acc.unit_dir, u) for u in units]
            return self._job(acc, f"{acc.name}: remove {unit}", unit, [
                (sc("disable", "--", *units), None), (["rm", "-f", "--", *paths], None), (sc("daemon-reload"), None)])

        key = body.get("stack")
        if not isinstance(key, str) or ":" not in key or not host.valid_name(key.split(":", 1)[1]):
            raise ApiError("bad stack")
        snap, st = self.stack(acc, key)
        name, is_pod, unit = st["name"], st["kind"] == "pod", st["unit"]
        pm = ["podman", "pod"] if is_pod else ["podman"]
        unit_name = (unit or {}).get("name")
        unit_active = bool(unit and unit["active"] in ("active", "activating", "reloading"))
        default_unit = f"{'pod' if is_pod else 'container'}-{name}.service"
        title = f"{acc.name}/{name}: {str(op).replace('_', ' ')}"

        def generate():
            # By ID: a pod often contains a container of the same name, and podman resolves a name to the
            # container first, which would produce only container-<name>.service.
            return [(["mkdir", "-p", acc.unit_dir], None),
                    (["podman", "generate", "systemd", "--new", "--files", "--name", st["id"]], acc.unit_dir),
                    (sc("daemon-reload"), None)]

        def recreate_via_unit():
            if unit_active:
                return [(sc("restart", unit_name), None)]
            return [(pm + ["stop", "--ignore", name], None), (pm + ["rm", "-f", "--ignore", name], None),
                    (sc("start", unit_name), None)]

        if op == "start":
            steps = [(sc("start", unit_name), None)] if unit_name else [(pm + ["start", name], None)]
        elif op == "stop":
            steps = [(sc("stop", unit_name), None)] if unit_active else [(pm + ["stop", name], None)]
        elif op == "restart":
            steps = [(sc("restart", unit_name), None)] if unit_active else [(pm + ["restart", name], None)]
        elif op == "adopt":
            if not unit_name:
                raise ApiError("no unit to hand over to")
            steps = [(pm + ["stop", "--ignore", name], None), (pm + ["rm", "-f", "--ignore", name], None),
                     (sc("start", unit_name), None)]
        elif op == "autostart_on":
            steps = ([] if unit_name else generate()) + [(sc("enable", unit_name or default_unit), None)]
        elif op == "autostart_off":
            if not unit_name:
                raise ApiError("no unit")
            steps = [(sc("disable", unit_name), None)]
        elif op == "regenerate_unit":
            if st["running"] == 0:
                raise ApiError("start it first: the unit is generated from the running definition")
            steps = generate() + ([(sc("enable", unit_name or default_unit), None)] if st["autostart"] else [])
        elif op == "remove":
            if st["running"]:
                raise ApiError("stop it first")
            steps = ([(sc("disable", unit_name), None)] if unit and unit["file_state"] == "enabled" else []) + \
                    [(pm + ["rm", "-f", name], None)]
        elif op == "rollback":
            if not st["rollback"]:
                raise ApiError("no previous image kept for this stack")
            if not unit_name:
                raise ApiError("rollback recreates the stack through its systemd unit; enable start on boot first")
            steps = [(["podman", "tag", host.rollback_ref(ref), ref], None) for ref in st["rollback"]]
            steps += recreate_via_unit()
        elif op in ("update", "run_script"):
            steps = self._update_steps(acc, st, body, op, recreate_via_unit)
        else:
            raise ApiError("unknown op")
        return self._job(acc, title, key, steps)

    def _update_steps(self, acc, st, body, op, recreate_via_unit):
        scripts = st["scripts"]
        if op == "run_script":
            path, arg = body.get("script"), body.get("arg", "")
            sc = next((s for s in scripts if s["path"] == path), None)
            if not sc or arg not in ("", "update"):
                raise ApiError("unknown script or argument")
            return [(["bash", sc["path"]] + ([arg] if arg else []), os.path.dirname(sc["path"]))]
        keep = self._keep_steps(st)
        sc = next((s for s in scripts if s["update_arg"]), None)
        if sc:
            return keep + [(["bash", sc["path"], "update"], os.path.dirname(sc["path"]))]
        if st["custom_images"]:
            raise ApiError("custom image without a launcher script: rebuild it manually")
        if not st["unit"]:
            raise ApiError("no launcher script and no systemd unit: enable start on boot first so the "
                           "panel can recreate it from the unit")
        pulls = [(["podman", "pull", ref], None) for ref in sorted({c["image"] for c in st["containers"]})]
        return keep + pulls + recreate_via_unit()

    @staticmethod
    def _keep_steps(st):
        """Tag the images that run now, so the update can be rolled back."""
        steps, seen = [], set()
        for c in st["containers"]:
            ref, rb = c["image"], host.rollback_ref(c["image"])
            if rb and ref not in seen and c["image_id"]:
                seen.add(ref)
                steps.append((["podman", "tag", c["image_id"], rb], None))
        return steps

    def _self_update_steps(self):
        if not self.is_root:
            raise ApiError("self-update needs the agent to run as root")
        src = self.config["source_dir"]
        installer = os.path.join(src, "install.sh")
        for p in (src, installer, os.path.join(src, "podman_panel")):
            try:
                st = os.lstat(p)
            except OSError:
                raise ApiError(f"{p} is missing")
            # Never run code as root that anyone but root could have changed.
            if st.st_uid != 0 or st.st_mode & (stat.S_IWGRP | stat.S_IWOTH) or stat.S_ISLNK(st.st_mode):
                raise ApiError(f"{p} must be owned by root and not group/world writable")
        steps = []
        if os.path.isdir(os.path.join(src, ".git")):
            steps.append((["git", "-C", src, "pull", "--ff-only"], src))
        # Run outside the agent's cgroup: the installer restarts the agent at its end.
        steps.append((["systemd-run", "--unit=podman-panel-upgrade", "--collect", "--quiet", "--wait", "--pipe",
                       "--working-directory", src, "/bin/bash", installer, "--upgrade"], src))
        return steps

    def _job(self, acc, title, target, steps):
        try:
            job = self.jobs.start(acc, title, target, steps, on_done=self.invalidate)
        except ValueError as e:
            raise ApiError(str(e), 409)
        return {"job": job.id}

    # -- read-only views
    def logs(self, q):
        acc = self.account(q.get("account"))
        name = q.get("container", "")
        if not host.valid_name(name):
            raise ApiError("bad container")
        try:
            tail = str(min(max(int(q.get("tail") or 300), 10), 10000))
        except ValueError:
            raise ApiError("bad tail")
        r = acc.run(["podman", "logs", "--tail", tail, "--timestamps", "--", name], timeout=30)
        return {"output": r.stdout + r.stderr}

    def stats(self, q):
        acc = self.account(q.get("account"))
        try:
            data = acc.podman_json("stats", "--no-stream", "-a", timeout=40)
        except host.CommandError as e:
            raise ApiError(str(e))
        return {"stats": data or []}

    def unit_text(self, q):
        acc = self.account(q.get("account"))
        unit = q.get("unit", "")
        if not host.valid_name(unit) or not unit.endswith(".service"):
            raise ApiError("bad unit")
        r = acc.run(acc.systemctl_argv("cat", "--", unit), timeout=20)
        return {"output": r.stdout or r.stderr}

    def inspect(self, q):
        acc = self.account(q.get("account"))
        name = q.get("container", "")
        if not host.valid_name(name):
            raise ApiError("bad container")
        data = acc.podman_json("container", "inspect", "--", name)
        d = data[0] if data else {}
        cfg = d.get("Config") or {}
        hc = d.get("HostConfig") or {}
        env = [e.split("=", 1)[0] + "=…" for e in cfg.get("Env") or []]  # never ship secret values
        return {"name": name, "image": d.get("ImageName"), "created": d.get("Created"),
                "command": (cfg.get("Entrypoint") or []) + (cfg.get("Cmd") or []),
                "env": env, "mounts": [{"src": m.get("Source"), "dst": m.get("Destination"), "type": m.get("Type"),
                                       "rw": m.get("RW")} for m in d.get("Mounts") or []],
                "restart": (hc.get("RestartPolicy") or {}).get("Name"),
                "network": hc.get("NetworkMode"),
                "labels": {k: v for k, v in (cfg.get("Labels") or {}).items() if not k.startswith("org.opencontainers")}}


# ---- unix-socket HTTP --------------------------------------------------------------------------
def make_handler(panel):
    allowed_uids = {0, os.geteuid()}
    try:
        allowed_uids.add(pwd.getpwnam(panel.config["web_user"]).pw_uid)
    except KeyError:
        pass

    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = "podman-panel-agent"

        def address_string(self):
            return "unix"

        def log_message(self, fmt, *args):
            pass  # changes go to the audit log; reads are not worth logging

        def _send(self, code, body):
            data = json.dumps(body, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _body(self):
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                raise ApiError("bad length")
            if n > 100_000:
                raise ApiError("body too large", 413)
            try:
                return json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                raise ApiError("bad json")

        def _peer_ok(self):
            try:
                creds = self.connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
                _, uid, _ = struct.unpack("3i", creds)
            except OSError:
                return False
            return uid in allowed_uids

        def _dispatch(self, method):
            if not self._peer_ok():
                raise ApiError("forbidden", 403)
            url = urllib.parse.urlsplit(self.path)
            q = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
            path = url.path
            client = (self.headers.get("X-Client") or "?")[:200]
            token = self.headers.get("X-Session") or ""

            if path == "/web-config" and method == "GET":
                return self._send(200, {"allowed_hosts": panel.config["allowed_hosts"]})
            if path == "/login" and method == "POST":
                b = self._body()
                try:
                    tok = panel.auth.login(b.get("password"), b.get("code"), client)
                except AuthError as e:
                    panel.audit(client, "login_failed", reason=str(e))
                    raise ApiError(str(e), e.code)
                panel.audit(client, "login")
                return self._send(200, {"token": tok})
            if not panel.auth.check(token):
                raise ApiError("login required", 401)
            if path == "/logout" and method == "POST":
                panel.auth.logout(token)
                panel.audit(client, "logout")
                return self._send(200, {"ok": True})

            if method == "GET":
                if path == "/state":
                    return self._send(200, panel.state(force=q.get("force") == "1"))
                if path == "/logs":
                    return self._send(200, panel.logs(q))
                if path == "/stats":
                    return self._send(200, panel.stats(q))
                if path == "/unit":
                    return self._send(200, panel.unit_text(q))
                if path == "/inspect":
                    return self._send(200, panel.inspect(q))
                if path == "/audit":
                    return self._send(200, {"entries": panel.audit_tail()})
                if path == "/jobs":
                    return self._send(200, {"jobs": panel.jobs.list()})
                if path.startswith("/jobs/"):
                    job = panel.jobs.get(_int(path.split("/")[2]))
                    if not job:
                        raise ApiError("no such job", 404)
                    off = max(_int(q.get("offset")), 0)
                    return self._send(200, {**job.summary(), "output": job.output[off:], "offset": len(job.output)})
            if method == "POST":
                if path == "/action":
                    return self._send(200, panel.action(self._body(), client))
                if path == "/check-updates":
                    panel.check_updates()
                    return self._send(200, {"ok": True})
                if path == "/logout-all":
                    n = panel.auth.logout_all()
                    panel.audit(client, "logout_all", sessions=n)
                    return self._send(200, {"ok": True})
                if path.startswith("/jobs/") and path.endswith("/cancel"):
                    job = panel.jobs.get(_int(path.split("/")[2]))
                    if not job:
                        raise ApiError("no such job", 404)
                    job.cancel()
                    panel.audit(client, "cancel_job", job=job.id, title=job.title)
                    return self._send(200, {"ok": True})
            raise ApiError("not found", 404)

        def _handle(self, method):
            try:
                self._dispatch(method)
            except ApiError as e:
                self._send(e.code, {"error": str(e)})
            except host.CommandError as e:
                self._send(500, {"error": str(e)})
            except Exception as e:  # keep serving; report the failure to the client
                print(f"unhandled: {e!r}", flush=True)
                self._send(500, {"error": "internal error"})

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

    return Handler


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return -1


class UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


def serve(config):
    if not config.get("password_hash") or not config.get("totp_secret"):
        raise SystemExit("No password or TOTP secret. Run: python3 -m podman_panel setup")
    panel = Panel(config)
    path = panel.config["socket"]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        os.unlink(path)
    old = os.umask(0o117)
    try:
        srv = UnixServer(path, make_handler(panel))
    finally:
        os.umask(old)
    if panel.is_root:
        try:
            gid = pwd.getpwnam(panel.config["web_user"]).pw_gid
        except KeyError:
            raise SystemExit(f"user {panel.config['web_user']} does not exist; run install.sh")
        d = os.path.dirname(path)
        os.chown(d, 0, gid)
        os.chmod(d, 0o750)
        os.chown(path, 0, gid)
    os.chmod(path, 0o660)
    threading.Thread(target=panel.update_loop, daemon=True).start()
    print(f"podman-panel agent on {path}  managing: {', '.join(panel.accounts)}", flush=True)
    srv.serve_forever()

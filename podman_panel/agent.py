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
import shlex
import pwd
import socket
import socketserver
import stat
import struct
import threading
import time
import urllib.parse

from . import backups, host, insights, registry, stackgen
from .auth import Auth, AuthError
from .jobs import JobManager

SNAP_TTL = 5
UPDATE_INTERVAL = 6 * 3600
BACKUP_INTERVAL = 600
DEFAULTS = {
    "socket": "/run/podman-panel/agent.sock",
    "web_user": "podman-panel",
    "audit_log": "/var/log/podman-panel/audit.log",
    "source_dir": "/opt/podman-panel",
    "allowed_hosts": [],
}


def rm_step(pm, name):
    """Remove a pod/container, ignoring a failure if it is gone anyway: podman 4.9 sometimes removes a pod
    and still exits with "removing pod … cgroup: Unit … slice not loaded"."""
    exists = ["podman", "pod", "exists"] if pm[-1] == "pod" else ["podman", "container", "exists"]
    return (["sh", "-c", shlex.join(pm + ["rm", "-f", "--ignore"]) + ' -- "$1" || ! ' + shlex.join(exists) + ' "$1"',
             "sh", name], None)


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
        self.backups = None  # backups.status(), refreshed in the background (Kopia can take seconds)
        self.backup_lock = threading.Lock()
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

    def _check_account(self, s):
        """Registry and git status of one account's snapshot."""
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
        return {"checked": time.time(), "images": images, "sources": sources}

    def _image_job_done(self, job):
        """After a job that pulls, builds or retags images, re-check that account, so an update does not
        keep being offered until the next scheduled check."""
        self.invalidate()
        if job.status == "succeeded" and job.account in self.updates:
            def recheck():
                acc = self.accounts[job.account]
                self.updates[job.account] = self._check_account(host.snapshot(acc))
            threading.Thread(target=recheck, daemon=True).start()

    def _check_updates(self):
        try:
            for s in self.snapshots(force=True):
                self.updates[s["account"]] = self._check_account(s)
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

    # -- backups (read-only)
    def refresh_backups(self):
        if self.is_root:
            threading.Thread(target=self._refresh_backups, daemon=True).start()

    def _refresh_backups(self):
        if not self.backup_lock.acquire(blocking=False):
            return
        try:
            res = backups.status({a.name: a.home for a in self.accounts.values()})
            res["checked"] = time.time()
            self.backups = res
        except Exception as e:  # never let a Kopia surprise stop the loop
            self.backups = {"installed": True, "connected": False, "error": str(e)[:300], "checked": time.time(),
                            "sources": [], "uncovered": [], "timer": None}
        finally:
            self.backup_lock.release()

    def backup_loop(self):
        while True:
            self._refresh_backups()
            time.sleep(BACKUP_INTERVAL)

    # -- state
    def state(self, force=False):
        snaps = self.snapshots(force)
        if force:
            self.refresh_backups()
        h = host.host_info()
        return {"host": h, "accounts": snaps, "updates": self.updates, "update_state": self.update_state,
                "insights": insights.build(h, snaps, self.updates, self.is_root, self.backups),
                "backups": self.backups,
                "jobs": self.jobs.list()[:30], "is_root": self.is_root,
                "panel": {"source_dir": self.config["source_dir"], "source": self.panel_source}}

    # -- actions
    def action(self, body, client):
        if not isinstance(body, dict):
            raise ApiError("bad request")
        op = body.get("op")
        if op == "create_stack":
            return self._create_stack(body, client)
        if op == "update_all":
            return self._update_all(body, client)
        if op == "self_update":
            me = host.Account(pwd.getpwuid(os.geteuid()).pw_name)
            result = self._job(me, "panel: update", "panel", self._self_update_steps())
            self.audit(client, "action", op=op, account=me.name, job=result["job"])
            return result
        acc = self.account(body.get("account"))
        result = self._action(op, acc, body)
        self.audit(client, "action", op=op, account=acc.name,
                   **{k: body[k] for k in ("stack", "container", "unit", "script", "arg") if isinstance(body.get(k), str)},
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
        if isinstance(op, str) and op.startswith("ctr_"):
            cname = body.get("container")
            return self._job(acc, f"{acc.name}/{st['name']}/{cname}: {op[4:].replace('_', ' ')}",
                             f"{key}/{cname}", self._container_steps(acc, st, cname, op))
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
                    (["podman", "generate", "systemd", "--new", "--files", "--restart-sec", "10", "--name", st["id"]], acc.unit_dir),
                    (sc("daemon-reload"), None)]

        recreate_via_unit = self._recreate_steps(acc, st)

        if op == "start":
            steps = [(sc("start", unit_name), None)] if unit_name else [(pm + ["start", name], None)]
        elif op == "stop":
            # reset-failed: podman's pod cleanup can exit with an error ("slice not loaded") after a clean
            # stop, which would leave the unit shown as failed instead of stopped.
            steps = ([(sc("stop", unit_name), None), (sc("reset-failed", unit_name), None)] if unit_active
                     else [(pm + ["stop", name], None)])
        elif op == "restart":
            steps = [(sc("restart", unit_name), None)] if unit_active else [(pm + ["restart", name], None)]
        elif op == "adopt":
            if not unit_name:
                raise ApiError("no unit to hand over to")
            steps = [(pm + ["stop", "--ignore", name], None), rm_step(pm, name),
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
                    [rm_step(pm, name)]
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
        return self._job(acc, title, key, steps, images_change=op in ("update", "run_script", "rollback"))

    def stack_spec(self, body):
        """Validate a new-stack request; returns (account, normalised spec, script text, env text)."""
        acc = self.account(body.get("account"))
        if acc.rootful:
            # A stack defined from the browser and run as root could bind-mount the host: root for any session.
            raise ApiError("new stacks can only be created for non-root users")
        try:
            spec = stackgen.validate(body.get("spec"), acc.home)
        except stackgen.SpecError as e:
            raise ApiError(str(e))
        snap = next(s for s in self.snapshots(force=True) if s["account"] == acc.name)
        names = {s["name"] for s in snap["stacks"]} | {c["name"] for s in snap["stacks"] for c in s["containers"]}
        clash = ({spec["name"]} | {c["name"] for c in spec["containers"]}) & names
        if clash:
            raise ApiError(f"already in use by {acc.name}: {', '.join(sorted(clash))}")
        if not os.path.exists("/opt/podman-panel/stacklib.sh") and self.is_root:
            raise ApiError("/opt/podman-panel/stacklib.sh is missing; rerun install.sh")
        script, env = stackgen.render(spec)
        return acc, spec, script, env

    def _create_stack(self, body, client):
        acc, spec, script, env = self.stack_spec(body)
        for path in (spec["script"], spec["env_file"]):
            if os.path.lexists(path):
                raise ApiError(f"{path} already exists")
        host.write_new_file(acc, spec["env_file"], env, "600")
        host.write_new_file(acc, spec["script"], script, "700")
        self.audit(client, "create_stack", account=acc.name, stack=spec["name"], script=spec["script"],
                   images=[c["image"] for c in spec["containers"]])
        self.invalidate()
        if not body.get("start"):
            return {"job": None, "script": spec["script"]}
        res = self._job(acc, f"{acc.name}/{spec['name']}: create", f"pod:{spec['name']}",
                        [(["bash", spec["script"], "update"], os.path.dirname(spec["script"]))], images_change=True)
        return {**res, "script": spec["script"]}

    def _update_all(self, body, client):
        """Every stack the last update check found outdated, queued one at a time per account
        (builds and pulls in parallel would fight over disk and CPU). Each stack is its own job, so
        one failure does not stop the rest."""
        only = body.get("account")
        if only is not None:
            self.account(only)
        snaps = self.snapshots(force=True)
        wanted = {(i["account"], i["stack"]) for i in insights.build(host.host_info(), snaps, self.updates, self.is_root)
                  if i["severity"] == "update" and i["action"] and (only is None or i["account"] == only)}
        jobs, skipped = [], []
        for account, key in sorted(wanted):
            acc = self.accounts[account]
            st = next((s for s in next(s for s in snaps if s["account"] == account)["stacks"] if s["key"] == key), None)
            if not st:
                continue
            try:
                steps = self._update_steps(acc, st, {}, "update", self._recreate_steps(acc, st))
                job = self.jobs.start(acc, f"{account}/{st['name']}: update", key, steps,
                                      on_done=self._image_job_done, queued=True)
                jobs.append(job.id)
            except (ApiError, ValueError) as e:
                skipped.append(f"{account}/{st['name']}: {e}")
        self.audit(client, "action", op="update_all", account=only or "*", jobs=jobs, skipped=skipped)
        if not jobs and not skipped:
            raise ApiError("nothing to update; run an update check first")
        return {"jobs": jobs, "skipped": skipped}

    @staticmethod
    def _container_steps(acc, st, cname, op):
        """One container of a pod. A container with its own unit (generated with --new) is controlled
        through systemd; stopping it removes it, and its unit brings it back on start."""
        c = next((c for c in st["containers"] if c["name"] == cname), None) if host.valid_name(cname) else None
        if not c or st["kind"] != "pod":
            raise ApiError("unknown container in this pod")
        sc = acc.systemctl_argv
        unit = c["unit"] if c["unit"] and os.path.exists(os.path.join(acc.unit_dir, c["unit"])) else None
        running = c["state"] == "running"
        if op == "ctr_start":
            if c["with_pod"] is False:
                raise ApiError("it is set not to start with its pod: switch 'Start with pod' on first")
            return [(sc("start", unit), None)] if unit else [(["podman", "start", cname], None)]
        if op == "ctr_stop":
            return [(sc("stop", unit), None)] if unit else [(["podman", "stop", cname], None)]
        if op == "ctr_restart":
            if unit:
                return [(sc("restart", unit), None)]
            return [(["podman", "restart", cname], None)]
        if op in ("ctr_with_pod_off", "ctr_with_pod_on"):
            if not unit:
                raise ApiError("this container has no unit of its own: it always starts with the pod")
            dropin = host.container_disabled_path(acc, unit)
            if op == "ctr_with_pod_off":
                write = ['sh', '-c', 'mkdir -p "$(dirname "$1")" && printf "%s" "$2" > "$1"', 'sh', dropin,
                         host.CONTAINER_DISABLED]
                return [(write, None), (sc("daemon-reload"), None)]
            return [(["rm", "-f", "--", dropin], None), (sc("daemon-reload"), None)]
        if op == "ctr_remove":
            if running:
                raise ApiError("stop it first")
            if unit:
                raise ApiError("its unit recreates it with the pod: switch 'Start with pod' off instead, and "
                               "remove it from the stack's script to drop it for good")
            return [rm_step(["podman"], cname)]
        raise ApiError("unknown op")

    def _recreate_steps(self, acc, st):
        unit = st["unit"]
        if not unit:
            return lambda: []
        pm = ["podman", "pod"] if st["kind"] == "pod" else ["podman"]
        sc = acc.systemctl_argv
        if unit["active"] in ("active", "activating", "reloading"):
            return lambda: [(sc("restart", unit["name"]), None)]
        return lambda: [(pm + ["stop", "--ignore", st["name"]], None), rm_step(pm, st["name"]),
                        (sc("start", unit["name"]), None)]

    def _update_steps(self, acc, st, body, op, recreate_via_unit):
        scripts = st["scripts"]
        if op == "run_script":
            path, arg = body.get("script"), body.get("arg", "")
            sc = next((s for s in scripts if s["path"] == path), None)
            if not sc or arg not in ("", "update"):
                raise ApiError("unknown script or argument")
            return self._script_steps(acc, st, sc, arg)
        keep = self._keep_steps(st)
        sc = next((s for s in scripts if s["update_arg"]), None)
        if sc:
            return keep + self._script_steps(acc, st, sc, "update")
        if st["custom_images"]:
            raise ApiError("custom image without a launcher script: rebuild it manually")
        if not st["unit"]:
            raise ApiError("no launcher script and no systemd unit: enable start on boot first so the "
                           "panel can recreate it from the unit")
        pulls = [(["podman", "pull", ref], None) for ref in sorted({c["image"] for c in st["containers"]})]
        return keep + pulls + recreate_via_unit()

    @staticmethod
    def _script_steps(acc, st, script, arg):
        """Run a launcher script so that systemd and the script do not fight over the stack.

        An older script removes and recreates the pod itself. If the stack has a unit, systemd sees that as
        a crash and recreates the pod at once (then the script's 'pod create' fails), or the new pod runs
        outside systemd. So: stop the unit, run the script, regenerate the unit from the pod the script
        made, and hand the pod back to systemd. Scripts on stacklib.sh do all of this themselves."""
        run = [(["bash", script["path"]] + ([arg] if arg else []), os.path.dirname(script["path"]))]
        unit = st["unit"]
        if script.get("stacklib") or not unit or (unit.get("path") or "").startswith("/run/"):
            return run  # no unit, or a generated (Quadlet) unit that cannot be regenerated from the pod
        sc = acc.systemctl_argv
        kind = "pod" if st["kind"] == "pod" else "container"
        # By ID at run time: the script creates a new pod, and a container may share the pod's name.
        regenerate = ('set -e; id=$(podman %s inspect --format "{{.Id}}" -- "$1"); cd "$2"; '
                      'podman generate systemd --new --files --restart-sec 10 --name "$id" >/dev/null; echo "regenerated units in $2"'
                      % kind)
        pm = ["podman", "pod"] if kind == "pod" else ["podman"]
        # If the script fails, bring the stack back through its previous unit rather than leaving it down.
        guarded = 'bash "$1" ${2:+"$2"} || { rc=$?; echo "script failed (exit $rc): starting the previous unit"; ' \
                  + shlex.join(pm + ["rm", "-f", "--ignore", st["name"]]) + " >/dev/null 2>&1; " \
                  + shlex.join(sc("start", unit["name"])) + '; exit "$rc"; }'
        run = [(["sh", "-c", guarded, "sh", script["path"], arg], os.path.dirname(script["path"]))]
        return ([(sc("stop", unit["name"]), None)] + run +
                [(["mkdir", "-p", acc.unit_dir], None),
                 (["sh", "-c", regenerate, "sh", st["name"], acc.unit_dir], None),
                 (pm + ["stop", "--ignore", st["name"]], None), rm_step(pm, st["name"]),
                 (sc("daemon-reload"), None), (sc("start", unit["name"]), None)])

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

    def _job(self, acc, title, target, steps, images_change=False):
        try:
            job = self.jobs.start(acc, title, target, steps,
                                  on_done=self._image_job_done if images_change else self.invalidate)
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
                if path == "/stack-preview":
                    _, spec, script, env = panel.stack_spec(self._body())
                    return self._send(200, {"script": script, "path": spec["script"], "env_file": spec["env_file"],
                                            "secret_keys": [k for c in spec["containers"] for k in c["secrets"]]})
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
    if panel.is_root:
        threading.Thread(target=panel.backup_loop, daemon=True).start()
    print(f"podman-panel agent on {path}  managing: {', '.join(panel.accounts)}", flush=True)
    srv.serve_forever()

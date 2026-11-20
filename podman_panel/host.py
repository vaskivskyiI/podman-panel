"""Access to podman, systemd and launcher scripts of every managed account.

When the panel runs as root it manages root (rootful podman) and every lingering user, running each
command as that user via runuser. When it runs as a normal user it manages only that user.
"""
import glob
import json
import os
import pwd
import re
import shutil
import subprocess
import time

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
SAFE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
LINGER_DIR = "/var/lib/systemd/linger"


class CommandError(Exception):
    pass


ROLLBACK_SUFFIX = "-pp-rollback"


def rollback_ref(ref):
    """The tag that keeps the previous image of `ref` across an update, e.g. postgres:16 -> postgres:16-pp-rollback."""
    if not ref or "@" in ref:
        return None
    name, tag = (ref.rsplit(":", 1) if ":" in ref.rsplit("/", 1)[-1] else (ref, "latest"))
    tag += ROLLBACK_SUFFIX
    return f"{name}:{tag}" if len(tag) <= 128 else None


def valid_name(name):
    return isinstance(name, str) and bool(NAME_RE.match(name))


class Account:
    def __init__(self, name):
        pw = pwd.getpwnam(name)
        self.name, self.uid, self.home = name, pw.pw_uid, pw.pw_dir
        self.rootful = self.uid == 0

    # ---- process execution -------------------------------------------------------------
    def env(self, extra=None):
        env = {"HOME": self.home, "USER": self.name, "LOGNAME": self.name, "PATH": SAFE_PATH,
               "LANG": "C.UTF-8", "GIT_TERMINAL_PROMPT": "0", "GIT_SSH_COMMAND": "ssh -o BatchMode=yes"}
        if not self.rootful:
            env["XDG_RUNTIME_DIR"] = f"/run/user/{self.uid}"
            env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path=/run/user/{self.uid}/bus"
        if extra:
            env.update(extra)
        return env

    def argv(self, argv):
        if os.geteuid() == self.uid:
            return list(argv)
        return ["runuser", "-u", self.name, "--"] + list(argv)

    def run(self, argv, timeout=60, cwd=None):
        try:
            return subprocess.run(self.argv(argv), env=self.env(), cwd=cwd or self.home,
                                  capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired as e:
            raise CommandError(f"timed out after {timeout}s: {' '.join(argv)}") from e
        except OSError as e:
            raise CommandError(f"cannot run {argv[0]}: {e.strerror}") from e

    def check(self, argv, timeout=60, cwd=None):
        r = self.run(argv, timeout, cwd)
        if r.returncode != 0:
            raise CommandError((r.stderr or r.stdout).strip() or f"exit code {r.returncode}")
        return r.stdout

    def popen(self, argv, cwd=None):
        return subprocess.Popen(self.argv(argv), env=self.env(), cwd=cwd or self.home,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                text=True, errors="replace", bufsize=1, start_new_session=True)

    def podman_json(self, *args, timeout=60):
        out = self.check(["podman", *args, "--format", "json"], timeout)
        return json.loads(out) if out.strip() else []

    def systemctl_argv(self, *args):
        return ["systemctl", *args] if self.rootful else ["systemctl", "--user", *args]

    def systemctl(self, *args, timeout=60):
        return self.check(self.systemctl_argv(*args), timeout)

    # ---- locations ---------------------------------------------------------------------
    @property
    def unit_dir(self):
        return "/etc/systemd/system" if self.rootful else os.path.join(self.home, ".config/systemd/user")

    @property
    def quadlet_dir(self):
        return "/etc/containers/systemd" if self.rootful else os.path.join(self.home, ".config/containers/systemd")

    @property
    def lingering(self):
        return self.rootful or os.path.exists(os.path.join(LINGER_DIR, self.name))


def managed_accounts(configured=None):
    """Accounts this process can manage: itself, or (as root) root plus every lingering/configured user."""
    if os.geteuid() != 0:
        return [Account(pwd.getpwuid(os.geteuid()).pw_name)]
    names = list(configured or [])
    if not configured:
        names = ["root"] + sorted(os.listdir(LINGER_DIR)) if os.path.isdir(LINGER_DIR) else ["root"]
    accounts = []
    for n in names:
        try:
            accounts.append(Account(n))
        except KeyError:
            pass
    return accounts


# ---- launcher scripts -------------------------------------------------------------------
POD_VAR_RE = re.compile(r'^\s*(?:POD_NAME|POD)=["\']?(?:\$\{\w+:-)?([A-Za-z0-9_.-]+)', re.M)
HOME_REF_RE = re.compile(r'(?:\$HOME|\$\{HOME\}|~)/([A-Za-z0-9_.-]+)')


def find_scripts(acc):
    """Launcher scripts (~/podman/*.sh, ~/<project>/deploy/*.sh) with the pod they manage and the
    source directories (with a Containerfile/Dockerfile or git checkout) they reference."""
    paths = sorted(glob.glob(os.path.join(acc.home, "podman", "*.sh")))
    paths += sorted(glob.glob(os.path.join(acc.home, "*", "deploy", "*.sh")))
    scripts = []
    for p in paths:
        if os.path.islink(p) or not os.path.isfile(p):
            continue
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                text = f.read(200_000)
        except OSError:
            continue
        pods = sorted(set(POD_VAR_RE.findall(text)))
        if not pods or "pod create" not in text:  # only launchers that (re)create the pod
            continue
        dirs = set()
        if os.path.basename(os.path.dirname(p)) == "deploy":
            dirs.add(os.path.dirname(os.path.dirname(p)))
        for seg in HOME_REF_RE.findall(text):
            d = os.path.join(acc.home, seg)
            if seg not in ("podman", "podman_data") and is_source_dir(d):
                dirs.add(d)
        scripts.append({"path": p, "pods": pods, "sources": sorted(dirs),
                        "update_arg": bool(re.search(r'\bupdate\b', text))})
    return scripts


def is_source_dir(d):
    if not os.path.isdir(d) or os.path.islink(d):
        return False
    return any(os.path.exists(os.path.join(d, n)) for n in (".git", "Containerfile", "Dockerfile"))


def source_status(acc, d, fetch):
    """Git state of a custom image's source checkout."""
    st = {"path": d, "git": os.path.isdir(os.path.join(d, ".git"))}
    if not st["git"]:
        return st
    if fetch:
        r = acc.run(["git", "-C", d, "fetch", "--quiet"], timeout=45)
        st["fetch_error"] = r.stderr.strip()[-300:] if r.returncode else None
    r = acc.run(["git", "-C", d, "log", "-1", "--format=%ct%x09%h%x09%s"], timeout=20)
    if r.returncode == 0 and r.stdout.strip():
        ts, h, subj = (r.stdout.strip().split("\t", 2) + ["", ""])[:3]
        st.update(commit_time=int(ts), commit=h, subject=subj)
    r = acc.run(["git", "-C", d, "rev-list", "--count", "HEAD..@{u}"], timeout=20)
    st["behind"] = int(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip().isdigit() else None
    r = acc.run(["git", "-C", d, "status", "--porcelain"], timeout=20)
    st["dirty"] = len(r.stdout.splitlines()) if r.returncode == 0 else None
    return st


# ---- systemd ------------------------------------------------------------------------------
def unit_states(acc, units):
    if not units:
        return {}
    out = acc.systemctl("show", "-p", "Id,LoadState,ActiveState,SubState,UnitFileState,FragmentPath",
                        "--", *sorted(units))
    states = {}
    for block in out.strip().split("\n\n"):
        kv = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
        if kv.get("Id"):
            states[kv["Id"]] = {
                "load": kv.get("LoadState"), "active": kv.get("ActiveState"), "sub": kv.get("SubState"),
                "file_state": kv.get("UnitFileState") or None, "path": kv.get("FragmentPath") or None}
    return states


def wanted_units(path):
    """Units listed in Wants=/Requires= of a generated pod unit (its container units)."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read(100_000)
    except OSError:
        return []
    names = []
    for m in re.finditer(r"^(?:Wants|Requires)=(.*)$", text, re.M):
        names += [u for u in m.group(1).split() if u.startswith("container-") and valid_name(u)]
    return names


# ---- snapshot -----------------------------------------------------------------------------
def snapshot(acc):
    """Everything the UI and the insights need about one account."""
    snap = {"account": acc.name, "uid": acc.uid, "rootful": acc.rootful, "lingering": acc.lingering,
            "error": None, "stacks": [], "images": [], "df": [], "unit_files": []}
    try:
        containers = acc.podman_json("ps", "-a", "--pod")
        pods = acc.podman_json("pod", "ps")
        images = acc.podman_json("images")
        snap["df"] = acc.podman_json("system", "df")
    except (CommandError, ValueError) as e:
        snap["error"] = str(e)
        return snap

    ids = [c["Id"] for c in containers]
    details = {d["Id"]: d for d in acc.podman_json("container", "inspect", *ids)} if ids else {}

    unit_files = sorted(os.path.basename(p) for p in
                        glob.glob(os.path.join(acc.unit_dir, "pod-*.service")) +
                        glob.glob(os.path.join(acc.unit_dir, "container-*.service")))
    quadlets = []
    for p in glob.glob(os.path.join(acc.quadlet_dir, "*")):
        stem, ext = os.path.splitext(os.path.basename(p))
        if ext in (".container", ".kube"):
            quadlets.append(stem + ".service")
        elif ext == ".pod":
            quadlets.append(stem + "-pod.service")
    labelled = {(c.get("Labels") or {}).get("PODMAN_SYSTEMD_UNIT") for c in containers} - {None, ""}
    states = unit_states(acc, set(unit_files) | set(quadlets) | labelled |
                         {"podman-restart.service", "podman-auto-update.timer"})
    snap["unit_files"] = unit_files
    snap["podman_restart_enabled"] = states.get("podman-restart.service", {}).get("file_state") == "enabled"
    snap["auto_update_timer"] = states.get("podman-auto-update.timer", {}).get("file_state")

    image_by_id, id_by_name = {}, {}
    for im in images:
        names = im.get("Names") or im.get("RepoTags") or []
        entry = {"id": im["Id"][:12], "full_id": im["Id"], "names": names, "size": im.get("Size", 0),
                 "created": im.get("Created", 0), "containers": im.get("Containers", 0),
                 "dangling": not names,
                 "digests": (im.get("RepoDigests") or []) + ([im["Digest"]] if im.get("Digest") else [])}
        snap["images"].append(entry)
        image_by_id[im["Id"]] = entry
        for n in names:
            id_by_name[n] = im["Id"][:12]

    scripts = find_scripts(acc)

    def ctr_view(c):
        d = details.get(c["Id"], {})
        cfg, hc = d.get("Config") or {}, d.get("HostConfig") or {}
        img = image_by_id.get(c.get("ImageID", ""), {})
        ports = [{"host_ip": p.get("host_ip") or "0.0.0.0", "host": p.get("host_port"),
                  "ctr": p.get("container_port"), "proto": p.get("protocol"), "range": p.get("range", 1)}
                 for p in (c.get("Ports") or [])]
        return {
            "id": c["Id"][:12], "name": c["Names"][0], "image": c.get("Image"), "image_id": c.get("ImageID", "")[:12],
            "image_created": img.get("created"), "state": c.get("State"), "status": c.get("Status"),
            "infra": c.get("IsInfra", False), "pod": c.get("PodName") or None,
            "started": c.get("StartedAt"), "exited_at": c.get("ExitedAt"), "created": c.get("Created"),
            "unit": (c.get("Labels") or {}).get("PODMAN_SYSTEMD_UNIT") or None,
            "autoupdate": (c.get("Labels") or {}).get("io.containers.autoupdate") or None,
            "health": ((d.get("State") or {}).get("Health") or {}).get("Status") or None,
            "has_healthcheck": bool(cfg.get("Healthcheck")),
            "restart_policy": (hc.get("RestartPolicy") or {}).get("Name") or None,
            "privileged": bool(hc.get("Privileged")), "network_mode": hc.get("NetworkMode"),
            "ports": ports, "restarts": c.get("Restarts", 0),
        }

    def unit_view(name):
        if not name:
            return None
        st = states.get(name)
        if not st or st["load"] == "not-found":
            return None
        return {"name": name, **st}

    by_pod = {}
    for c in containers:
        by_pod.setdefault(c.get("PodName") or None, []).append(ctr_view(c))

    for p in pods:
        ctrs = by_pod.get(p["Name"], [])
        unit = unit_view(f"pod-{p['Name']}.service") or unit_view(
            next((c["unit"] for c in ctrs if c["infra"] and c["unit"]), None))
        snap["stacks"].append(_stack("pod", p["Name"], p["Id"][:12], p.get("Status"), ctrs, unit, scripts, id_by_name))
    for c in by_pod.get(None, []):
        unit = unit_view(c["unit"]) or unit_view(f"container-{c['name']}.service")
        snap["stacks"].append(_stack("container", c["name"], c["id"], c["state"], [c], unit, scripts, id_by_name))

    # Units on disk whose pod/container no longer exists.
    present = {s["unit"]["name"] for s in snap["stacks"] if s["unit"]}
    live = {f"pod-{s['name']}.service" for s in snap["stacks"]} | {
        f"container-{c['name']}.service" for s in snap["stacks"] for c in s["containers"]}
    snap["orphan_units"] = [
        {"name": u, **states.get(u, {})} for u in unit_files
        if u.startswith("pod-") and u not in present and u not in live]
    snap["stacks"].sort(key=lambda s: (s["kind"] != "pod", s["name"]))
    return snap


def _stack(kind, name, ident, status, ctrs, unit, scripts, id_by_name):
    members = [c for c in ctrs if not c["infra"]]
    running = sum(c["state"] == "running" for c in members)
    own = [s for s in scripts if name in s["pods"]]
    # Previous images kept by the last update, where they differ from what runs now.
    rollback = sorted({c["image"] for c in members
                       if id_by_name.get(rollback_ref(c["image"])) not in (None, c["image_id"])})
    return {
        "key": f"{kind}:{name}", "kind": kind, "name": name, "id": ident, "status": status,
        "containers": members, "running": running, "total": len(members),
        "unit": unit,
        "autostart": bool(unit and unit["file_state"] in ("enabled", "generated", "static")),
        "scripts": own,
        "sources": sorted({d for s in own for d in s["sources"]}),
        "rollback": rollback,
        "custom_images": sorted({c["image"] for c in members if (c["image"] or "").startswith("localhost/")}),
    }


def host_info():
    du = shutil.disk_usage("/")
    mem = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                k, v = line.split(":", 1)
                mem[k] = int(v.split()[0]) * 1024
    except OSError:
        pass
    try:
        load = os.getloadavg()
    except OSError:
        load = (0, 0, 0)
    try:
        with open("/proc/uptime") as f:
            uptime = float(f.read().split()[0])
    except OSError:
        uptime = 0
    return {"hostname": os.uname().nodename, "disk_total": du.total, "disk_used": du.used, "disk_free": du.free,
            "mem_total": mem.get("MemTotal", 0), "mem_available": mem.get("MemAvailable", 0),
            "load": load, "cpus": os.cpu_count(), "uptime": uptime, "time": time.time(),
            "podman": shutil.which("podman") is not None}

"""Read-only status of the host's Kopia backups (see backup/README.md), if Kopia is installed.

Reads the repository root is connected to (/root/.config/kopia) and the kopia-backup timer. Never
returns connection details: only the storage type, the sources and their snapshots.
"""
import datetime
import json
import os
import shutil
import subprocess

from .host import SAFE_PATH

UNIT = "kopia-backup"
STACK_DIRS = ("podman", "podman_data", "podman_logs")
ENV = {"HOME": "/root", "PATH": SAFE_PATH, "LANG": "C.UTF-8", "KOPIA_CHECK_FOR_UPDATES": "false"}


def _run(argv, timeout=60):
    try:
        r = subprocess.run(argv, env=ENV, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, f"timed out after {timeout}s"
    except OSError as e:
        return None, e.strerror
    if r.returncode != 0:
        lines = (r.stderr or r.stdout).strip().splitlines()
        return None, lines[-1] if lines else f"exit code {r.returncode}"
    return r.stdout, None


def _time(s):
    """Kopia's RFC 3339 times (nanoseconds, Z) -> unix seconds."""
    if not s:
        return None
    s = s.replace("Z", "+00:00")
    if "." in s:
        head, rest = s.split(".", 1)
        i = next((k for k, c in enumerate(rest) if not c.isdigit()), len(rest))
        s = f"{head}.{rest[:i][:6]}{rest[i:]}"
    try:
        return datetime.datetime.fromisoformat(s).timestamp()
    except ValueError:
        return None


def _units():
    out, _ = _run(["systemctl", "show", f"{UNIT}.service", f"{UNIT}.timer", "--timestamp=unix",
                   "-p", "Id,LoadState,ActiveState,Result,ExecMainExitTimestamp,ExecMainStatus,"
                         "LastTriggerUSec,NextElapseUSecRealtime"], timeout=20)
    units = {}
    for block in (out or "").strip().split("\n\n"):
        props = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
        if props.get("Id"):
            units[props["Id"]] = props

    def ts(v):
        return float(v[1:]) if v and v.startswith("@") else None

    svc, tmr = units.get(f"{UNIT}.service", {}), units.get(f"{UNIT}.timer", {})
    if tmr.get("LoadState") != "loaded":
        return None
    return {"enabled": tmr.get("ActiveState") == "active",
            "running": svc.get("ActiveState") == "activating",
            "result": svc.get("Result"), "exit_status": svc.get("ExecMainStatus"),
            "last_run": ts(tmr.get("LastTriggerUSec")), "last_exit": ts(svc.get("ExecMainExitTimestamp")),
            "next_run": ts(tmr.get("NextElapseUSecRealtime"))}


def status(homes):
    """homes: {account: home dir} of managed accounts, to report stack folders no source covers."""
    if not shutil.which("kopia", path=SAFE_PATH):
        return {"installed": False}
    res = {"installed": True, "connected": False, "timer": _units(), "sources": [], "uncovered": []}
    out, err = _run(["kopia", "repository", "status", "--json"])
    if err:
        res["error"] = err
        return res
    try:
        repo = json.loads(out)
    except ValueError:
        repo = {}
    opts = repo.get("clientOptions") or {}
    res.update(connected=True, storage=(repo.get("connectionInfo") or {}).get("type"),
               readonly=bool(repo.get("readonly") or opts.get("readonly")),
               actions=bool(opts.get("enableActions")))

    out, err = _run(["kopia", "snapshot", "list", "--all", "--json"], timeout=120)
    if err:
        res["error"] = err
        return res
    try:
        snaps = json.loads(out) or []
    except ValueError:
        snaps = []
    by_src = {}
    for s in snaps:
        src = s.get("source") or {}
        key = (src.get("userName"), src.get("host"), src.get("path"))
        summ = (s.get("rootEntry") or {}).get("summ") or {}
        stats = s.get("stats") or {}
        by_src.setdefault(key, []).append({
            "start": _time(s.get("startTime")), "end": _time(s.get("endTime")),
            "size": summ.get("size", stats.get("totalSize", 0)), "files": summ.get("files", stats.get("fileCount", 0)),
            "errors": summ.get("numFailed", stats.get("errorCount", 0)) or 0,
            "incomplete": bool(s.get("incompleteReason"))})
    for (user, hostname, path), items in sorted(by_src.items(), key=lambda kv: kv[0][2] or ""):
        items.sort(key=lambda x: x["start"] or 0)
        res["sources"].append({"user": user, "host": hostname, "path": path, "count": len(items),
                               "first": items[0]["start"], "latest": items[-1]})

    me = opts.get("hostname") or os.uname().nodename
    covered = {src["path"] for src in res["sources"] if src["host"] == me}
    for acc, home in homes.items():
        for d in STACK_DIRS:
            p = os.path.join(home, d)
            if os.path.isdir(p) and p not in covered:
                res["uncovered"].append({"account": acc, "path": p})
    return res

"""Improvement suggestions derived from snapshots, update checks and host state."""
import time

from .host import ROLLBACK_SUFFIX

DB_PORTS = {5432: "PostgreSQL", 3306: "MySQL/MariaDB", 6379: "Redis", 27017: "MongoDB", 11211: "Memcached"}
DAY = 86400
GB = 1024 ** 3


def _i(severity, account, title, detail, action=None, stack=None):
    return {"severity": severity, "account": account, "title": title, "detail": detail,
            "action": action, "stack": stack}


def build(host, snaps, updates, is_root):
    out = []
    now = time.time()
    used = host["disk_used"] / host["disk_total"] if host["disk_total"] else 0
    if used > 0.85:
        out.append(_i("warn", None, f"Root filesystem is {used:.0%} full",
                      f"{host['disk_free'] / GB:.0f} GB free. Pruning unused images below is the quickest win."))

    for s in snaps:
        a = s["account"]
        if s.get("error"):
            out.append(_i("warn", a, "Cannot read podman state", s["error"]))
            continue

        reclaim = next((d.get("RawReclaimable", 0) for d in s["df"] if d.get("Type") == "Images"), 0)
        dangling = [im for im in s["images"] if im["dangling"] and not im["containers"]]
        if dangling:
            size = sum(im["size"] for im in dangling)
            out.append(_i("warn" if size > 5 * GB else "info", a,
                          f"{len(dangling)} dangling images ({size / GB:.1f} GB)",
                          "Untagged leftovers of rebuilds and pulls. Removing them never affects a container.",
                          {"op": "prune_dangling", "account": a}))
        unused = [im for im in s["images"] if not im["dangling"] and not im["containers"]
                  and not any(n.startswith("localhost/podman-pause") or n.endswith(ROLLBACK_SUFFIX)
                              for n in im["names"])]
        if unused and reclaim > 2 * GB:
            out.append(_i("info", a, f"{len(unused)} tagged images not used by any container",
                          ", ".join(n for im in unused[:8] for n in im["names"][:1]) +
                          (" …" if len(unused) > 8 else "") +
                          f". Total reclaimable: {reclaim / GB:.1f} GB. Base images of your own builds are among "
                          "them; they are re-pulled on the next build.",
                          {"op": "prune_unused", "account": a}))

        has_units = any(st["autostart"] for st in s["stacks"])
        if has_units and not s["lingering"]:
            out.append(_i("warn", a, "Lingering is off",
                          "User services only start at boot with lingering enabled.",
                          {"op": "enable_linger", "account": a} if is_root else None))

        for o in s.get("orphan_units", []):
            out.append(_i("info", a, f"Unit {o['name']} has no pod",
                          f"Generated unit for a pod that no longer exists (enabled: {o.get('file_state')}). "
                          "Remove it together with its container units.",
                          {"op": "remove_orphan_unit", "account": a, "unit": o["name"]}))

        if s.get("auto_update_timer") != "enabled" and any(
                c["autoupdate"] for st in s["stacks"] for c in st["containers"]):
            out.append(_i("info", a, "Containers have auto-update labels but the timer is off",
                          "podman-auto-update.timer is not enabled, so io.containers.autoupdate does nothing."))

        unit_generated = False
        no_health = []
        for st in s["stacks"]:
            name, key = st["name"], st["key"]
            running = st["running"] > 0
            unit = st["unit"]
            if unit:
                unit_generated = True
            restart_policy = any(c["restart_policy"] in ("always", "unless-stopped") for c in st["containers"])

            if running and not st["autostart"]:
                via = " (restart policy alone needs podman-restart.service, which is disabled)" \
                    if restart_policy and not s["podman_restart_enabled"] else ""
                out.append(_i("warn", a, f"{name} will not start after reboot",
                              f"Running, but no enabled systemd unit{via}.",
                              {"op": "autostart_on", "account": a, "stack": key}, key))
            elif running and unit and unit["active"] != "active":
                out.append(_i("warn", a, f"{name} runs outside its systemd unit",
                              f"{unit['name']} is {unit['active']} while the {st['kind']} runs. It was started "
                              "by a script, so systemd neither supervises nor restarts it. Restarting through "
                              "systemd recreates it from the unit (short downtime).",
                              {"op": "adopt", "account": a, "stack": key}, key))

            if not running and st["status"] in ("Exited", "Created", "exited", "created", "Stopped"):
                last = max([c.get("exited_at") or 0 for c in st["containers"]] +
                           [c.get("created") or 0 for c in st["containers"]])
                if last and now - last > 14 * DAY and not st["autostart"]:
                    out.append(_i("info", a, f"{name} has been stopped for {int((now - last) / DAY)} days",
                                  "Remove it if it is no longer used (volumes and bind-mounted data are kept).",
                                  {"op": "remove", "account": a, "stack": key}, key))

            for c in st["containers"]:
                ref = c["image"] or ""
                tag = ref.rsplit(":", 1)[-1] if ":" in ref.rsplit("/", 1)[-1] else "latest"
                if running and c["state"] == "running":
                    for p in c["ports"]:
                        if p["ctr"] in DB_PORTS and p["host_ip"] in ("0.0.0.0", "", "::"):
                            out.append(_i("warn", a, f"{DB_PORTS[p['ctr']]} of {c['name']} is published on all interfaces",
                                          f"Host port {p['host']} is reachable from the network. Inside a pod the app reaches "
                                          "the database on localhost, so the port can usually be dropped or bound to 127.0.0.1.",
                                          stack=key))
                    if not c["has_healthcheck"]:
                        no_health.append(c["name"])
                    if c["privileged"]:
                        out.append(_i("info", a, f"{c['name']} runs --privileged",
                                      "Prefer specific --device / --cap-add over full privileges.", stack=key))
                if tag == "latest" and any(k in ref for k in ("postgres", "mariadb", "mysql", "mongo", "pgvector")):
                    out.append(_i("warn", a, f"{c['name']} uses a database image with :latest",
                                  "A pull can jump a major version and the data directory will not start. Pin the major version.",
                                  stack=key))
                created = c.get("image_created") or 0
                if created and now - created > 365 * DAY and not ref.startswith("localhost/"):
                    upd = updates.get(a, {}).get("images", {}).get(ref, {})
                    if upd.get("status") != "outdated":
                        out.append(_i("tip", a, f"{c['name']}: image is {int((now - created) / DAY / 30)} months old",
                                      f"{ref} has the newest digest for its tag, but the tag itself seems unmaintained "
                                      "or pinned. Check whether a newer tag exists.", stack=key))

        if no_health:
            out.append(_i("tip", a, f"{len(no_health)} running containers have no healthcheck",
                          ", ".join(sorted(no_health)) + ". A --health-cmd lets podman (and this panel) notice a "
                          "hung service; with --health-on-failure=restart podman also restarts it."))

        if unit_generated and not any(st["unit"] and st["unit"]["path"] and "/generator" in st["unit"]["path"]
                                      for st in s["stacks"]):
            out.append(_i("tip", a, "Units are made with 'podman generate systemd'",
                          "That command is deprecated in favour of Quadlet (.container/.pod files in "
                          "~/.config/containers/systemd). Quadlet units survive podman upgrades and support "
                          "AutoUpdate=registry. The current units keep working."))

        upd = updates.get(a, {})
        for ref, r in upd.get("images", {}).items():
            if r.get("status") == "outdated":
                stacks = [st for st in s["stacks"] if any(c["image"] == ref for c in st["containers"])]
                for st in stacks:
                    out.append(_i("update", a, f"Update available: {ref}", f"Used by {st['name']}.",
                                  {"op": "update", "account": a, "stack": st["key"]}, st["key"]))
        for path, src in upd.get("sources", {}).items():
            for st in s["stacks"]:
                if path not in st["sources"]:
                    continue
                img_time = min((c.get("image_created") or 0) for c in st["containers"] if
                               (c["image"] or "").startswith("localhost/")) if st["custom_images"] else 0
                if src.get("behind"):
                    out.append(_i("update", a, f"{st['name']}: {src['behind']} new commits upstream",
                                  f"{path} is behind its remote. Updating pulls and rebuilds.",
                                  {"op": "update", "account": a, "stack": st["key"]}, st["key"]))
                elif img_time and src.get("commit_time", 0) > img_time + 60:
                    out.append(_i("update", a, f"{st['name']}: image is older than the source",
                                  f"Last commit {src.get('commit')} ({src.get('subject', '')[:60]}) is newer than "
                                  "the running image. Rebuild to deploy it.",
                                  {"op": "update", "account": a, "stack": st["key"]}, st["key"]))

    order = {"warn": 0, "update": 1, "info": 2, "tip": 3}
    out.sort(key=lambda x: (order[x["severity"]], x["account"] or "", x["title"]))
    return out

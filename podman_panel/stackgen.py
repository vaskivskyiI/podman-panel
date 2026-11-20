"""Generate a launcher script in the standard layout (see stacklib.sh) from a validated stack spec.

The spec comes from the browser, so every field is checked against a strict pattern and every value is
shell-quoted. There are no free-form podman arguments.
"""
import ipaddress
import re
import shlex

from .host import valid_name

IMAGE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*(?::[0-9]+)?(?:/[a-z0-9][a-z0-9._-]*)+(?::[A-Za-z0-9_][A-Za-z0-9_.-]{0,127})?"
                      r"(?:@sha256:[a-f0-9]{64})?$|^localhost/[a-z0-9][a-z0-9._/-]*(?::[A-Za-z0-9_][A-Za-z0-9_.-]{0,127})?$")
PATH_RE = re.compile(r"^/(?:[A-Za-z0-9_.@+-]+/?)*$")
DEVICE_RE = re.compile(r"^/dev/[A-Za-z0-9_./-]+$")
ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
VOLUME_OPTS = {"ro", "rw", "Z", "z", "U"}
LIB = "/opt/podman-panel/stacklib.sh"


class SpecError(ValueError):
    pass


def _path(p, what):
    if not isinstance(p, str) or not PATH_RE.match(p) or "/../" in p + "/" or "/./" in p + "/" or len(p) > 4096:
        raise SpecError(f"{what}: must be an absolute path without '..' ({p!r})")
    return p.rstrip("/") or "/"


def _value(v, what):
    if not isinstance(v, str) or len(v) > 4096 or any(c in v for c in "\n\r\0"):
        raise SpecError(f"{what}: values must be single-line text up to 4096 characters")
    return v


def validate(spec, home):
    """Return a normalised copy of the spec, or raise SpecError."""
    if not isinstance(spec, dict):
        raise SpecError("bad spec")
    name = spec.get("name")
    if not valid_name(name) or len(name) > 63 or not name[0].isalpha() or not name.islower():
        raise SpecError("name: lowercase letters, digits, '.', '_' or '-', starting with a letter")
    bind = spec.get("bind") or "127.0.0.1"
    try:
        ipaddress.ip_address(bind)
    except ValueError:
        raise SpecError("bind: must be an IP address, e.g. 127.0.0.1 or 0.0.0.0")
    ports = []
    for p in spec.get("ports") or []:
        try:
            host, ctr, proto = int(p["host"]), int(p["container"]), p.get("proto") or "tcp"
        except (KeyError, TypeError, ValueError):
            raise SpecError("ports: host and container must be numbers")
        if not (1 <= host <= 65535 and 1 <= ctr <= 65535) or proto not in ("tcp", "udp"):
            raise SpecError(f"ports: {host}:{ctr}/{proto} is out of range")
        ports.append({"host": host, "container": ctr, "proto": proto})
    containers, seen = [], set()
    raw = spec.get("containers") or []
    if not isinstance(raw, list) or not 1 <= len(raw) <= 10:
        raise SpecError("containers: between 1 and 10")
    for c in raw:
        if not isinstance(c, dict):
            raise SpecError("bad container")
        cname = c.get("name")
        if not valid_name(cname) or cname in seen or cname == name:
            raise SpecError(f"container name {cname!r}: must be unique, valid, and differ from the pod name")
        seen.add(cname)
        image = c.get("image")
        if not isinstance(image, str) or not IMAGE_RE.match(image):
            raise SpecError(f"{cname}: image must be a full reference like docker.io/library/nginx:1.27")
        vols = []
        for v in c.get("volumes") or []:
            src, dst, opts = v.get("source"), v.get("target"), v.get("options") or []
            if isinstance(src, str) and src.startswith("/"):
                src = _path(src, f"{cname}: volume source")
            elif not valid_name(src):
                raise SpecError(f"{cname}: volume source must be an absolute path or a volume name")
            dst = _path(dst, f"{cname}: volume target")
            if not isinstance(opts, list) or not set(opts) <= VOLUME_OPTS:
                raise SpecError(f"{cname}: volume options are limited to {', '.join(sorted(VOLUME_OPTS))}")
            vols.append({"source": src, "target": dst, "options": opts})
        env, secrets = {}, {}
        for kind, target in (("env", env), ("secrets", secrets)):
            for k, v in (c.get(kind) or {}).items():
                if not ENV_KEY_RE.match(k):
                    raise SpecError(f"{cname}: bad variable name {k!r}")
                target[k] = _value(v, f"{cname}: {k}")
        devices = []
        for d in c.get("devices") or []:
            if not isinstance(d, str) or not DEVICE_RE.match(d) or ".." in d:
                raise SpecError(f"{cname}: devices must be /dev/… paths")
            devices.append(d)
        containers.append({"name": cname, "image": image, "volumes": vols, "env": env, "secrets": secrets,
                           "devices": devices})
    return {"name": name, "bind": bind, "ports": ports, "containers": containers,
            "script": f"{home}/podman/{name}.sh", "env_file": f"{home}/podman/{name}.env"}


def render(s):
    """The launcher script and the env file (secrets) for a validated spec."""
    q = shlex.quote
    out = [
        "#!/usr/bin/env bash",
        f"# {s['name']}: created with Podman Panel. Edit freely; keep POD, IMAGES and create().",
        f"#   {s['name']}.sh [up|update|down|status]     (see {LIB})",
        f"# Secrets: {s['name']}.env next to this script (mode 600), passed with --env-file.",
        "",
        f"POD={q(s['name'])}",
        "IMAGES=(",
        *[f"  {q(i)}" for i in dict.fromkeys(c["image"] for c in s["containers"])],
        ")",
        "",
        "create() {",
    ]
    dirs = sorted({v["source"] for c in s["containers"] for v in c["volumes"] if v["source"].startswith("/")})
    if dirs:
        out.append("  mkdir -p " + " ".join(q(d) for d in dirs))
    pod = ['  podman pod create --name "$POD"']
    for p in s["ports"]:
        bind = f"[{s['bind']}]" if ":" in s["bind"] else s["bind"]
        pod.append("-p " + q(f"{bind}:{p['host']}:{p['container']}/{p['proto']}"))
    out.append(" \\\n    ".join(pod))
    for c in s["containers"]:
        run = [f'  podman run -d --pod "$POD" --name {q(c["name"])}']
        if c["secrets"]:
            run.append('--env-file "$ENV_FILE"')
        run += ["-e " + q(f"{k}={v}") for k, v in c["env"].items()]
        for v in c["volumes"]:
            spec = f"{v['source']}:{v['target']}" + (":" + ",".join(v["options"]) if v["options"] else "")
            run.append("-v " + q(spec))
        run += ["--device " + q(d) for d in c["devices"]]
        run.append(q(c["image"]))
        out.append(" \\\n    ".join(run))
    out += ["}", "", f"source {LIB}", 'stack_main "$@"', ""]
    env = "".join(f"{k}={v}\n" for c in s["containers"] for k, v in c["secrets"].items())
    return "\n".join(out), env

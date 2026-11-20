"""Remote digest lookup over the OCI distribution API (anonymous pulls only), without skopeo."""
import hashlib
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

ACCEPT = ", ".join([
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.docker.distribution.manifest.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
])
UA = "podman-panel/1"


def parse_ref(ref):
    """'ghcr.io/a/b:tag' -> ('ghcr.io', 'a/b', 'tag'); None for digest-pinned or local images."""
    if not ref or "@" in ref:
        return None
    name, tag = ref, "latest"
    if ":" in ref.rsplit("/", 1)[-1]:
        name, tag = ref.rsplit(":", 1)
    first, _, rest = name.partition("/")
    if rest and ("." in first or ":" in first or first == "localhost"):
        registry, repo = first, rest
    else:
        registry, repo = "docker.io", name
    if registry == "localhost":
        return None
    if registry == "docker.io" and "/" not in repo:
        repo = "library/" + repo
    return registry, repo, tag


def _request(url, method, headers, timeout):
    req = urllib.request.Request(url, method=method, headers={"User-Agent": UA, **headers})
    return urllib.request.urlopen(req, timeout=timeout)


def _token(challenge, timeout):
    params = dict(re.findall(r'(\w+)="([^"]*)"', challenge))
    realm = params.pop("realm", None)
    if not realm or not realm.startswith("https://"):
        raise RuntimeError("registry requires unsupported authentication")
    query = urllib.parse.urlencode({k: v for k, v in params.items() if k in ("service", "scope")})
    with _request(f"{realm}?{query}", "GET", {}, timeout) as r:
        body = json.load(r)
    return body.get("token") or body.get("access_token")


ARCH = {"x86_64": "amd64", "aarch64": "arm64", "armv7l": "arm"}.get(os.uname().machine, os.uname().machine)
_platform_cache = {}  # index digest -> platform manifest digest


def _fetch(url, method, headers, timeout):
    """Request with one anonymous bearer-token retry. Returns (digest, body or None)."""
    for attempt in range(2):
        try:
            with _request(url, method, headers, timeout) as r:
                body = r.read() if method == "GET" else None
                digest = r.headers.get("Docker-Content-Digest") or (
                    "sha256:" + hashlib.sha256(body).hexdigest() if body is not None else None)
                return digest, body
        except urllib.error.HTTPError as e:
            if e.code == 401 and attempt == 0:
                challenge = e.headers.get("WWW-Authenticate", "")
                if not challenge.lower().startswith("bearer"):
                    raise RuntimeError("registry requires credentials") from e
                headers["Authorization"] = "Bearer " + _token(challenge, timeout)
                continue
            if e.code == 404:
                raise RuntimeError("tag not found in registry") from e
            raise RuntimeError(f"registry answered HTTP {e.code}") from e
    raise RuntimeError("registry authentication failed")


def check(registry, repo, tag, local_digests, timeout=15):
    """Compare a tag with the local image's digests.

    HEAD first (Docker Hub does not count it against the pull limit). Only when the tag's digest is not
    local, GET the index once to find this architecture's manifest: a multi-arch tag changes whenever any
    architecture is rebuilt, which is not an update for this host."""
    host = "registry-1.docker.io" if registry == "docker.io" else registry
    url = f"https://{host}/v2/{repo}/manifests/{tag}"
    headers = {"Accept": ACCEPT}
    digest, _ = _fetch(url, "HEAD", headers, timeout)
    if digest and digest in local_digests:
        return {"status": "current", "remote": digest}
    if not digest or digest not in _platform_cache:
        digest, body = _fetch(url, "GET", headers, timeout)
        manifest = json.loads(body or b"{}")
        platform = digest
        for m in manifest.get("manifests", []):
            p = m.get("platform") or {}
            if p.get("os") == "linux" and p.get("architecture") == ARCH:
                platform = m["digest"]
                break
        _platform_cache[digest] = platform
    platform = _platform_cache[digest]
    return {"status": "current" if platform in local_digests else "outdated", "remote": platform}

"""The unprivileged half: serves the UI and forwards /api/* to the agent's unix socket.

It runs as an unprivileged, sandboxed service, holds no secrets and keeps no state. It enforces the browser-side
rules (allowed Host names against DNS rebinding, same-origin POSTs with a custom header against CSRF,
a __Host- Secure HttpOnly SameSite=Strict cookie, strict security headers); the agent decides who is
signed in and what is allowed.
"""
import http.client
import http.server
import json
import os
import socket
import time
import urllib.parse

STATIC = os.path.join(os.path.dirname(__file__), "static")
COOKIE = "__Host-pp"
MIME = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml"}
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                               "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
                               "base-uri 'none'; form-action 'self'; object-src 'none'",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), usb=(), payment=()",
    "Cache-Control": "no-store",
}
LOCAL_HOSTS = {"localhost", "127.0.0.1", "[::1]"}  # for an ssh tunnel; a rebinding attack cannot send these


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, path, timeout):
        super().__init__("agent", timeout=timeout)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


class Agent:
    def __init__(self, path):
        self.path = path
        self._hosts, self._hosts_time = None, 0

    def call(self, method, path, body=None, headers=None, timeout=120):
        conn = UnixConnection(self.path, timeout)
        try:
            conn.request(method, path, body=body, headers={"Content-Type": "application/json", **(headers or {})})
            r = conn.getresponse()
            return r.status, r.read()
        finally:
            conn.close()

    def allowed_hosts(self):
        if self._hosts is None or time.time() - self._hosts_time > 60:
            try:
                _, data = self.call("GET", "/web-config", timeout=10)
                self._hosts = {h.lower() for h in json.loads(data).get("allowed_hosts", [])}
                self._hosts_time = time.time()
            except (OSError, ValueError):
                return self._hosts or set()
        return self._hosts


def make_handler(agent):
    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = "podman-panel"
        sys_version = ""

        def log_message(self, fmt, *args):
            if "/api/state" not in (args[0] if args else "") and "/api/jobs/" not in (args[0] if args else ""):
                super().log_message(fmt, *args)

        def _send(self, code, body, ctype="application/json", headers=None):
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            for k, v in {**SECURITY_HEADERS, **(headers or {})}.items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        def _cookie(self):
            for part in (self.headers.get("Cookie") or "").split(";"):
                k, _, v = part.strip().partition("=")
                if k == COOKIE:
                    return v[:200]
            return ""

        def _host_ok(self):
            hostname = (self.headers.get("Host") or "").strip().lower()
            bare = hostname.rsplit(":", 1)[0] if not hostname.endswith("]") else hostname
            return bare in LOCAL_HOSTS or hostname in agent.allowed_hosts() or bare in agent.allowed_hosts()

        def _same_origin(self):
            origin = self.headers.get("Origin")
            if origin is None:
                return True  # same-origin fetches may omit it; X-Panel still forces a preflight otherwise
            o = urllib.parse.urlsplit(origin)
            return o.scheme in ("https", "http") and o.netloc.lower() == (self.headers.get("Host") or "").lower()

        def _client(self):
            fwd = (self.headers.get("X-Forwarded-For") or "").split(",")[-1].strip()
            return self.client_address[0] + (f" (forwarded for {fwd})" if fwd else "")

        def _static(self, path):
            rel = "index.html" if path in ("/", "") else path.lstrip("/")
            full = os.path.realpath(os.path.join(STATIC, rel))
            if not full.startswith(os.path.realpath(STATIC) + os.sep) or not os.path.isfile(full):
                return self._send(404, {"error": "not found"})
            with open(full, "rb") as f:
                self._send(200, f.read(), MIME.get(os.path.splitext(full)[1], "application/octet-stream"))

        def _handle(self, method):
            if not self._host_ok():
                return self._send(421, {"error": "unknown host name; add it to allowed_hosts"})
            url = urllib.parse.urlsplit(self.path)
            if not url.path.startswith("/api/"):
                return self._static(url.path) if method in ("GET", "HEAD") else self._send(405, {"error": "method"})
            body = b""
            if method == "POST":
                if self.headers.get("X-Panel") != "1" or not self._same_origin():
                    return self._send(403, {"error": "cross-site request refused"})
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    n = -1
                if not 0 <= n <= 100_000:
                    return self._send(413, {"error": "body too large"})
                body = self.rfile.read(n)
            path = url.path[4:] + (f"?{url.query}" if url.query else "")
            try:
                status, data = agent.call(method, path, body=body or None,
                                          headers={"X-Session": self._cookie(), "X-Client": self._client()})
            except OSError:
                return self._send(503, {"error": "agent not reachable"})
            headers = {}
            if url.path == "/api/login" and status == 200:
                token = json.loads(data).get("token", "")
                headers["Set-Cookie"] = f"{COOKIE}={token}; Path=/; Secure; HttpOnly; SameSite=Strict"
                data = b'{"ok": true}'
            elif url.path in ("/api/logout", "/api/logout-all") or status == 401:
                headers["Set-Cookie"] = f"{COOKIE}=; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age=0"
            self._send(status, data, headers=headers)

        def do_GET(self):
            self._handle("GET")

        def do_HEAD(self):
            self._handle("HEAD")

        def do_POST(self):
            self._handle("POST")

    return Handler


def serve(socket_path, bind, port):
    agent = Agent(socket_path)
    srv = http.server.ThreadingHTTPServer((bind, port), make_handler(agent))
    srv.daemon_threads = True
    print(f"podman-panel web on http://{bind}:{port} -> {socket_path}", flush=True)
    srv.serve_forever()

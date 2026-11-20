"""Password + TOTP login, sessions and login throttling. Lives in the root agent, never in the web service."""
import base64
import hashlib
import hmac
import secrets
import struct
import threading
import time

SESSION_IDLE = 3600          # a closed tab logs out after an hour
SESSION_MAX = 12 * 3600      # and every session ends after 12 hours
FREE_FAILURES = 5            # failed logins before throttling starts
MAX_DELAY = 300


def hash_password(pw, iterations=600_000):
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${dk.hex()}"


def check_password(pw, stored):
    try:
        _, it, salt, h = stored.split("$")
        dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), int(it))
        return hmac.compare_digest(dk.hex(), h)
    except (ValueError, AttributeError):
        return False


def new_totp_secret():
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def totp_uri(secret, label):
    return f"otpauth://totp/Podman%20Panel:{label}?secret={secret}&issuer=Podman%20Panel&digits=6&period=30"


def _hotp(secret, counter):
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    mac = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    o = mac[-1] & 0x0F
    return "%06d" % ((struct.unpack(">I", mac[o:o + 4])[0] & 0x7FFFFFFF) % 1_000_000)


def totp_counter(secret, code, now=None):
    """The time step a code belongs to (±1 step of clock drift), or None."""
    code = (code or "").strip().replace(" ", "")
    if not (len(code) == 6 and code.isdigit()):
        return None
    step = int((now or time.time()) // 30)
    for c in (step, step - 1, step + 1):
        if hmac.compare_digest(_hotp(secret, c), code):
            return c
    return None


class AuthError(Exception):
    def __init__(self, msg, code=401):
        super().__init__(msg)
        self.code = code


class Auth:
    def __init__(self, config):
        self.config = config
        self.sessions = {}       # token hash -> {"created", "seen", "client"}
        self.failures = []       # timestamps of recent failed logins (all clients: there is one admin)
        self.last_counter = 0    # TOTP replay protection
        self.lock = threading.Lock()

    def login(self, password, code, client):
        with self.lock:
            now = time.time()
            self.failures = [t for t in self.failures if now - t < 900]
            if len(self.failures) >= FREE_FAILURES:
                wait = min(2 ** (len(self.failures) - FREE_FAILURES + 1), MAX_DELAY)
                left = self.failures[-1] + wait - now
                if left > 0:
                    raise AuthError(f"too many failed logins, wait {int(left) + 1} s", 429)
            ok_pw = check_password(password or "", self.config.get("password_hash", ""))
            counter = totp_counter(self.config.get("totp_secret", ""), code)
            if not ok_pw or counter is None or counter <= self.last_counter:
                self.failures.append(now)
                time.sleep(1)
                raise AuthError("wrong password or code")
            self.last_counter = counter
            self.failures.clear()
            token = secrets.token_urlsafe(32)
            self.sessions[_h(token)] = {"created": now, "seen": now, "client": client}
            return token

    def check(self, token):
        if not token:
            return False
        with self.lock:
            key = _h(token)
            s = self.sessions.get(key)
            now = time.time()
            if not s or now - s["seen"] > SESSION_IDLE or now - s["created"] > SESSION_MAX:
                self.sessions.pop(key, None)
                return False
            s["seen"] = now
            return True

    def logout(self, token):
        with self.lock:
            self.sessions.pop(_h(token or ""), None)

    def logout_all(self):
        with self.lock:
            n = len(self.sessions)
            self.sessions.clear()
            return n


def _h(token):
    return hashlib.sha256(token.encode()).hexdigest()

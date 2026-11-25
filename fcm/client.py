"""FortiCloud IAM OAuth + Asset Management V3 API client (standard library only)."""

import json
import os
import socket
import sys
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque

DEFAULT_AUTH_URL = "https://customerapiauth.fortinet.com/api/v1/oauth/token/"
DEFAULT_API_BASE = "https://support.fortinet.com/ES/api/registration/v3"
DEFAULT_CLIENT_ID = "assetmanagement"

USER_AGENT = "FortiCloud-Asset-Bulk-Manager/1.0"


_CA_CACHE = {"pem": None, "source": None}
_MAC_KEYCHAINS = [
    "/System/Library/Keychains/SystemRootCertificates.keychain",  # Apple's trusted roots
    "/Library/Keychains/System.keychain",                         # company / admin-installed CAs
]
_PEM_RE = None


def _macos_keychain_pem():
    """Export CA certificates from the macOS keychains as PEM text (cached)."""
    global _PEM_RE
    if _CA_CACHE["pem"] is not None:
        return _CA_CACHE["pem"]
    pem = ""
    if sys.platform == "darwin":
        import subprocess
        for kc in _MAC_KEYCHAINS:
            if not os.path.exists(kc):
                continue
            try:
                out = subprocess.run(["/usr/bin/security", "find-certificate", "-a", "-p", kc],
                                     capture_output=True, text=True, timeout=20)
                pem += out.stdout
            except Exception:
                pass
    _CA_CACHE["pem"] = pem
    return pem


def _add_pem(ctx, pem):
    """Load PEM certificates one by one, skipping any the ssl module rejects."""
    import re
    added = 0
    for block in re.findall(r"-----BEGIN CERTIFICATE-----.+?-----END CERTIFICATE-----", pem, re.S):
        try:
            ctx.load_verify_locations(cadata=block)
            added += 1
        except (ssl.SSLError, ValueError):
            continue
    return added


_CTX = None


def ssl_context():
    """Shared TLS context (built once)."""
    global _CTX
    if _CTX is None:
        _CTX = _build_context()
    return _CTX


def _build_context():
    """TLS context that trusts: Python's defaults, certifi (if installed), the macOS keychains
    (Apple roots + any company CA installed in the System keychain), and FCM_CA_BUNDLE if set."""
    ctx = ssl.create_default_context()
    sources = []
    if ctx.cert_store_stats().get("x509_ca"):
        sources.append("Python defaults")
    try:
        import certifi  # optional
        ctx.load_verify_locations(cafile=certifi.where())
        sources.append("certifi")
    except Exception:
        pass
    n = _add_pem(ctx, _macos_keychain_pem())
    if n:
        sources.append("macOS keychain (%d certs)" % n)
    extra = os.environ.get("FCM_CA_BUNDLE")
    if extra and os.path.exists(extra):
        try:
            ctx.load_verify_locations(cafile=extra)
            sources.append("FCM_CA_BUNDLE")
        except Exception:
            pass
    _CA_CACHE["source"] = ", ".join(sources) or "none"
    return ctx


def ca_sources():
    if _CA_CACHE["source"] is None:
        ssl_context()
    return _CA_CACHE["source"]


class ApiError(Exception):
    def __init__(self, message, http_status=None, body=None):
        super().__init__(message)
        self.http_status = http_status
        self.body = body


class RateLimiter:
    """Keeps calls under per-minute / per-hour caps and tracks API errors per rolling hour.

    Shared by every call this app makes, so jobs and connectivity tests count together.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.calls = deque()
        self.errors = deque()
        self.last_call = 0.0

    def _prune(self, now):
        while self.calls and now - self.calls[0] > 3600:
            self.calls.popleft()
        while self.errors and now - self.errors[0] > 3600:
            self.errors.popleft()

    def wait(self, per_minute, per_hour, min_delay, stop_event=None, on_wait=None):
        """Block until a call is allowed. Returns False if stopped while waiting."""
        while True:
            with self.lock:
                now = time.time()
                self._prune(now)
                last_min = sum(1 for t in self.calls if now - t < 60)
                wait_for = 0.0
                if per_hour and len(self.calls) >= per_hour:
                    wait_for = max(wait_for, 3600 - (now - self.calls[0]) + 0.5)
                if per_minute and last_min >= per_minute:
                    oldest_in_min = [t for t in self.calls if now - t < 60][0]
                    wait_for = max(wait_for, 60 - (now - oldest_in_min) + 0.5)
                if min_delay:
                    wait_for = max(wait_for, min_delay - (now - self.last_call))
                if wait_for <= 0:
                    self.calls.append(now)
                    self.last_call = now
                    return True
            if on_wait and wait_for > 3:
                on_wait(wait_for)
            step = min(wait_for, 1.0)
            if stop_event is not None:
                if stop_event.wait(step):
                    return False
            else:
                time.sleep(step)

    def record_error(self):
        with self.lock:
            self.errors.append(time.time())

    def stats(self):
        with self.lock:
            now = time.time()
            self._prune(now)
            return {
                "callsLastMinute": sum(1 for t in self.calls if now - t < 60),
                "callsLastHour": len(self.calls),
                "errorsLastHour": len(self.errors),
            }


def _http_post_json(url, payload, headers=None, timeout=60):
    """POST JSON, return (http_status, parsed_body_or_text, response_headers)."""
    data = json.dumps(payload).encode("utf-8")
    h = {"Content-Type": "application/json", "Accept": "application/json", "User-Agent": USER_AGENT}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h, method="POST")
    ctx = ssl_context() if url.lower().startswith("https") else None
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            raw = resp.read()
            status = resp.status
            rh = dict(resp.headers)
    except urllib.error.HTTPError as e:
        raw = e.read() or b""
        status = e.code
        rh = dict(e.headers or {})
    text = raw.decode("utf-8", "replace")
    try:
        body = json.loads(text) if text.strip() else {}
    except ValueError:
        body = text
    return status, body, rh


def scrub(body):
    """Remove token fields before anything is logged or shown."""
    if isinstance(body, dict):
        return {k: ("***" if k.lower() in ("token", "access_token", "refresh_token") else scrub(v))
                for k, v in body.items()}
    if isinstance(body, list):
        return [scrub(x) for x in body]
    return body


def api_message(body, http_status=None):
    if isinstance(body, dict):
        for key in ("message", "error", "error_description", "detail"):
            val = body.get(key)
            if val:
                return str(val) if not isinstance(val, (dict, list)) else json.dumps(val)
        return ""
    if isinstance(body, str):
        txt = body.strip()
        if txt.startswith("<"):
            return "HTTP %s (non-JSON response)" % http_status
        return txt[:300]
    return ""


def is_api_success(http_status, body):
    if http_status is None or http_status >= 400:
        return False
    if isinstance(body, dict):
        st = body.get("status")
        return st in (0, "0") or (st is None and not body.get("error"))
    return False


class FortiCloudClient:
    def __init__(self, api_id, password, client_id=DEFAULT_CLIENT_ID,
                 auth_url=DEFAULT_AUTH_URL, api_base=DEFAULT_API_BASE,
                 limiter=None, timeout=90):
        self.api_id = api_id
        self.password = password
        self.client_id = client_id or DEFAULT_CLIENT_ID
        self.auth_url = auth_url or DEFAULT_AUTH_URL
        self.api_base = (api_base or DEFAULT_API_BASE).rstrip("/")
        self.limiter = limiter or RateLimiter()
        self.timeout = timeout
        self._token = None
        self._token_expiry = 0.0
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- auth
    @property
    def revoke_url(self):
        return self.auth_url.replace("/oauth/token/", "/oauth/revoke_token/")

    def get_token(self, force=False):
        with self._lock:
            if self._token and not force and time.time() < self._token_expiry - 120:
                return self._token
            try:
                status, body, _ = _http_post_json(self.auth_url, {
                    "username": self.api_id,
                    "password": self.password,
                    "client_id": self.client_id,
                    "grant_type": "password",
                }, timeout=self.timeout)
            except urllib.error.URLError as e:
                raise ApiError("Cannot reach the FortiCloud authentication server: %s" % _reason(e))
            if status >= 400 or not isinstance(body, dict) or not body.get("access_token"):
                msg = api_message(body, status) or "HTTP %s" % status
                raise ApiError("Authentication failed: %s" % msg, status, scrub(body))
            self._token = body["access_token"]
            self._token_expiry = time.time() + float(body.get("expires_in") or 3600)
            return self._token

    def revoke(self):
        with self._lock:
            tok, self._token = self._token, None
        if not tok:
            return
        try:
            _http_post_json(self.revoke_url, {"client_id": self.client_id, "token": tok}, timeout=20)
        except Exception:
            pass

    @property
    def token_expires_in(self):
        if not self._token:
            return 0
        return max(0, int(self._token_expiry - time.time()))

    # ---------------------------------------------------------------- API calls
    def call(self, path, payload, settings=None, stop_event=None, on_wait=None, write_op=True):
        """Call an Asset Management endpoint. Returns (http_status, body).

        Retries on HTTP 429/502/503/504 and connection errors that happen before the request
        is sent. Read timeouts on write operations are NOT retried (the change may have applied).
        """
        s = settings or {}
        retries = int(s.get("retries", 2))
        attempt = 0
        refreshed = False
        while True:
            ok = self.limiter.wait(int(s.get("maxPerMinute", 90)), int(s.get("maxPerHour", 900)),
                                   float(s.get("delaySeconds", 1.0)), stop_event, on_wait)
            if not ok:
                raise ApiError("Stopped by user before the call was sent.")
            token = self.get_token()
            url = self.api_base + path
            try:
                status, body, headers = _http_post_json(
                    url, payload, {"Authorization": "Bearer " + token}, timeout=self.timeout)
            except (socket.timeout, TimeoutError) as e:
                if write_op or attempt >= retries:
                    raise ApiError("Timed out waiting for FortiCloud. The change may or may not have "
                                   "been applied - verify with 'Look up details'.")
                attempt += 1
                continue
            except urllib.error.URLError as e:
                reason = _reason(e)
                if "timed out" in reason.lower() and write_op:
                    raise ApiError("Timed out waiting for FortiCloud. The change may or may not have "
                                   "been applied - verify with 'Look up details'.")
                if attempt >= retries:
                    raise ApiError("Network error: %s" % reason)
                attempt += 1
                _sleep(stop_event, 5 * attempt)
                continue

            if status == 401 and not refreshed:
                refreshed = True
                self.get_token(force=True)
                continue
            if status in (429, 502, 503, 504) and attempt < retries:
                attempt += 1
                delay = _retry_after(headers) or (15 * attempt if status == 429 else 5 * attempt)
                if on_wait:
                    on_wait(delay)
                _sleep(stop_event, delay)
                continue
            if not is_api_success(status, body):
                self.limiter.record_error()
            return status, body


def _retry_after(headers):
    for k, v in (headers or {}).items():
        if k.lower() == "retry-after":
            try:
                return min(float(v), 300)
            except ValueError:
                return None
    return None


def _sleep(stop_event, seconds):
    if stop_event is not None:
        stop_event.wait(seconds)
    else:
        time.sleep(seconds)


def _reason(e):
    r = getattr(e, "reason", e)
    if isinstance(r, ssl.SSLCertVerificationError) or "CERTIFICATE_VERIFY_FAILED" in str(r):
        return ("TLS certificate verification failed (%s). Trusted CAs loaded: %s. If your network "
                "inspects HTTPS with a company certificate, ask IT for the root CA (.pem) and start the app "
                "with FCM_CA_BUNDLE=/path/to/ca.pem. With python.org Python you can also run "
                "'Install Certificates.command' from the Python folder in Applications." % (r, ca_sources()))
    return str(r)


# ---------------------------------------------------------------- connectivity checks

def check_host(url, timeout=10):
    """DNS + TCP + TLS check for the host of `url`. Returns (ok, detail)."""
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    t0 = time.time()
    try:
        addr = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)[0][4][0]
    except socket.gaierror as e:
        return False, "DNS lookup failed for %s (%s)" % (host, e)
    proxy = urllib.request.getproxies().get(parsed.scheme)
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            if parsed.scheme == "https":
                with ssl_context().wrap_socket(sock, server_hostname=host) as tls:
                    ver = tls.version()
                return True, "%s -> %s, %s OK (%d ms). Trusted CAs: %s" % (
                    host, addr, ver, (time.time() - t0) * 1000, ca_sources())
            return True, "%s -> %s:%s reachable (%d ms)" % (host, addr, port, (time.time() - t0) * 1000)
    except ssl.SSLError as e:
        return False, "TLS handshake with %s failed: %s" % (host, _reason(e))
    except OSError as e:
        hint = " (a proxy is configured: %s - direct connections may be blocked)" % proxy if proxy else ""
        return False, "Cannot connect to %s:%s - %s%s" % (host, port, e, hint)

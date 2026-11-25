#!/usr/bin/env python3
"""FortiCloud Asset Bulk Manager - local web interface.

Run:  python3 app.py            (real FortiCloud)
      python3 app.py --demo     (built-in fake FortiCloud, nothing is sent to Fortinet)

Only the Python standard library is used. The server listens on 127.0.0.1 only.
"""

import argparse
import base64
import json
import os
import secrets
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

if sys.version_info < (3, 8):
    sys.exit("Python 3.8 or newer is required.")

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from fcm import __version__, operations as ops  # noqa: E402
from fcm.client import (DEFAULT_API_BASE, DEFAULT_AUTH_URL, DEFAULT_CLIENT_ID, ApiError,  # noqa: E402
                        FortiCloudClient, RateLimiter, api_message, check_host, is_api_success, scrub)
from fcm.jobs import Job  # noqa: E402
from fcm.sheets import SheetError, read_table, write_csv, write_xlsx  # noqa: E402

MAX_UPLOAD = 20 * 1024 * 1024
MAX_ROWS = 5000

DEFAULT_SETTINGS = {
    "batchSize": 10,          # units per call (register is capped at 10 by FortiCloud)
    "delaySeconds": 1.0,      # minimum gap between calls
    "maxPerMinute": 90,       # FortiCloud documents 100/min
    "maxPerHour": 900,        # FortiCloud documents 1000/hour
    "maxErrorsPerHour": 8,    # FortiCloud documents 10 errors/hour
    "retries": 2,
    "stopOnError": False,
    "accountId": "",
}


class AppState:
    def __init__(self, demo=False, auth_url=None, api_base=None, results_dir=None):
        self.session_key = secrets.token_urlsafe(24)
        self.demo = demo
        self.auth_url = auth_url or DEFAULT_AUTH_URL
        self.api_base = api_base or DEFAULT_API_BASE
        self.limiter = RateLimiter()
        self.client = None
        self.connected_as = None
        self.datasets = {}
        self.jobs = {}
        self.active_job = None
        self.settings = dict(DEFAULT_SETTINGS)
        self.results_dir = results_dir or os.path.join(HERE, "results")
        self.lock = threading.Lock()


STATE = None


# --------------------------------------------------------------------------- helpers

def _proxy_configured():
    return bool(urllib.request.getproxies().get("https"))


def _settings(body):
    s = dict(STATE.settings)
    for k, v in (body.get("settings") or {}).items():
        if k not in DEFAULT_SETTINGS:
            continue
        try:
            if k in ("batchSize", "maxPerMinute", "maxPerHour", "maxErrorsPerHour", "retries"):
                v = max(0, int(v))
            elif k == "delaySeconds":
                v = max(0.0, float(v))
            elif k == "stopOnError":
                v = bool(v)
            elif k == "accountId":
                v = str(v).strip()
                if v and not v.isdigit():
                    raise ValueError("Account ID must be a number.")
        except (TypeError, ValueError) as e:
            raise ValueError("Invalid setting %s: %s" % (k, e))
        s[k] = v
    s["batchSize"] = max(1, s["batchSize"])
    s["maxPerMinute"] = min(max(1, s["maxPerMinute"]), 100)
    s["maxPerHour"] = min(max(1, s["maxPerHour"]), 1000)
    STATE.settings = s
    return s


def _make_client(body):
    api_id = (body.get("apiId") or "").strip()
    password = body.get("password") or ""
    if not api_id or not password:
        raise ValueError("Enter the API ID and password of your FortiCloud IAM API user.")
    return FortiCloudClient(api_id, password, (body.get("clientId") or DEFAULT_CLIENT_ID).strip(),
                            STATE.auth_url, STATE.api_base, STATE.limiter)


def _status():
    c = STATE.client
    return {
        "connected": bool(c and c._token),
        "apiId": STATE.connected_as,
        "tokenExpiresIn": c.token_expires_in if c else 0,
        "demo": STATE.demo,
        "authUrl": STATE.auth_url,
        "apiBase": STATE.api_base,
        "limits": STATE.limiter.stats(),
        "activeJob": STATE.active_job,
    }


# --------------------------------------------------------------------------- API handlers

def h_meta(_body):
    return {"version": __version__, "operations": ops.public_definitions(),
            "settings": STATE.settings, "status": _status(), "resultsDir": STATE.results_dir}


def h_status(_body):
    return _status()


def h_connect(body):
    client = _make_client(body)
    client.get_token()
    if STATE.client and STATE.client is not client:
        STATE.client.revoke()
    STATE.client = client
    STATE.connected_as = client.api_id
    return _status()


def h_disconnect(_body):
    if STATE.active_job and STATE.jobs[STATE.active_job].state == "running":
        raise ValueError("A job is running. Stop it before disconnecting.")
    if STATE.client:
        STATE.client.revoke()
    STATE.client = None
    STATE.connected_as = None
    return _status()


def h_test(body):
    """Connectivity test: network reach, authentication, and a read-only API call."""
    steps = []

    def add(name, ok, detail, level=None):
        steps.append({"name": name, "ok": ok, "level": level or ("ok" if ok else "error"), "detail": detail})

    for label, url in (("Reach FortiCloud IAM (authentication)", STATE.auth_url),
                       ("Reach FortiCloud Asset Management API", STATE.api_base)):
        ok, detail = check_host(url)
        if not ok and _proxy_configured():
            add(label, True, detail + " - continuing because a proxy is configured.", "warn")
        else:
            add(label, ok, detail)

    use_body_creds = bool((body.get("apiId") or "").strip() and body.get("password"))
    try:
        client = _make_client(body) if use_body_creds else STATE.client
    except ValueError as e:
        add("Authenticate (get OAuth token)", False, str(e))
        return {"steps": steps, "ok": False}
    if client is None:
        add("Authenticate (get OAuth token)", False, "Enter the API ID and password first.")
        return {"steps": steps, "ok": False}

    t0 = time.time()
    try:
        client.get_token(force=use_body_creds)
        add("Authenticate (get OAuth token)", True,
            "Token issued for client_id '%s' in %d ms, valid ~%d min. (API user %s)" % (
                client.client_id, (time.time() - t0) * 1000, client.token_expires_in // 60, client.api_id))
    except ApiError as e:
        hint = ""
        if e.http_status in (400, 401):
            hint = (" Check the API ID / password, and that the API user's permission profile includes "
                    "Asset Management with client_id '%s'." % client.client_id)
        add("Authenticate (get OAuth token)", False, str(e) + hint)
        return {"steps": steps, "ok": False}

    serial = (body.get("testSerial") or "").strip().upper()
    if serial:
        path, payload, label = "/products/details", {"serialNumber": serial}, "Read product %s" % serial
    else:
        path = "/products/list"
        payload = {"expireBefore": "2000-01-01T00:00:00"}
        label = "Read-only API call (/products/list)"
        acct = (body.get("settings") or {}).get("accountId") or STATE.settings.get("accountId")
        if acct:
            payload["accountId"] = int(acct)
    t0 = time.time()
    try:
        status, resp = client.call(path, payload, {"retries": 0, "delaySeconds": 0,
                                                   "maxPerMinute": 90, "maxPerHour": 900}, write_op=False)
        ms = (time.time() - t0) * 1000
        if is_api_success(status, resp):
            extra = ""
            if serial and isinstance(resp, dict):
                ad = resp.get("assetDetails") or {}
                extra = " - %s, registered %s" % (ad.get("productModel", "?"), (ad.get("registrationDate") or "?")[:10])
            add(label, True, "HTTP %s, API status 0 (success) in %d ms%s." % (status, ms, extra))
        elif status in (401, 403):
            add(label, False, "HTTP %s: %s. The token works but this API user may lack Asset Management "
                              "permission." % (status, api_message(resp, status) or "access denied"))
        elif status < 400 and isinstance(resp, dict):
            add(label, True, "Reached the API and it answered (HTTP %s, status %s): %s" % (
                status, resp.get("status"), api_message(resp, status) or "no message"), "warn")
        else:
            add(label, False, "HTTP %s: %s" % (status, api_message(resp, status)))
    except ApiError as e:
        add(label, False, str(e))

    if use_body_creds:
        # keep the tested credentials as the active connection
        if STATE.client and STATE.client is not client:
            STATE.client.revoke()
        STATE.client = client
        STATE.connected_as = client.api_id
    ok = all(s["ok"] for s in steps)
    return {"steps": steps, "ok": ok, "status": _status()}


def h_parse(body):
    op_key = body.get("op")
    if op_key not in ops.OPERATIONS:
        raise ValueError("Unknown operation.")
    if body.get("text"):
        data, name = body["text"], "pasted.txt"
    else:
        name = body.get("filename") or "upload"
        try:
            data = base64.b64decode(body.get("content") or "")
        except ValueError:
            raise ValueError("Could not read the uploaded file.")
        if len(data) > MAX_UPLOAD:
            raise ValueError("File is larger than 20 MB.")
    try:
        headers, rows = read_table(name, data)
    except SheetError as e:
        raise ValueError(str(e))
    if len(rows) > MAX_ROWS:
        raise ValueError("The file has %d rows; the limit is %d per run. Split it into smaller files."
                         % (len(rows), MAX_ROWS))
    if not rows:
        raise ValueError("No data rows found under the header row.")
    result = ops.validate(op_key, headers, rows)
    ds_id = uuid.uuid4().hex[:12]
    STATE.datasets[ds_id] = {"op": op_key, "filename": name, **result}
    return {"datasetId": ds_id, "filename": name, **_public_dataset(result)}


def _public_dataset(result):
    return {"columns": result["columns"], "unknownColumns": result["unknownColumns"],
            "missingColumns": result["missingColumns"], "summary": result["summary"],
            "rows": [{"row": r["row"], "raw": r["raw"], "errors": r["errors"],
                      "warnings": r["warnings"], "status": r["status"]} for r in result["rows"]]}


def _dataset(body):
    ds = STATE.datasets.get(body.get("datasetId"))
    if not ds:
        raise ValueError("Load a file first (the previous upload has expired).")
    return ds


def h_dryrun(body):
    ds = _dataset(body)
    s = _settings(body)
    reqs = ops.build_requests(ds["op"], ds["rows"], s)
    base = STATE.api_base
    return {"requests": [{"method": "POST", "url": base + r["path"], "payload": r["payload"],
                          "rows": r["rows"]} for r in reqs],
            "count": len(reqs), "serials": sum(len(r["serials"]) for r in reqs),
            "skipped": ds["summary"]["error"],
            "estimatedSeconds": int(len(reqs) * max(s["delaySeconds"], 60.0 / s["maxPerMinute"]))}


def h_submit(body):
    ds = _dataset(body)
    op = ops.OPERATIONS[ds["op"]]
    if not STATE.client or not STATE.client._token:
        raise ValueError("Connect to FortiCloud first.")
    if STATE.active_job and STATE.jobs[STATE.active_job].state == "running":
        raise ValueError("Another job is still running.")
    if op["confirmWord"] and (body.get("confirm") or "").strip().upper() != op["confirmWord"]:
        raise ValueError("Type %s to confirm." % op["confirmWord"])
    s = _settings(body)
    reqs = ops.build_requests(ds["op"], ds["rows"], s)
    if not reqs:
        raise ValueError("There are no valid rows to send.")
    job = Job(ds["op"], ds, reqs, s, STATE.client, STATE.results_dir)
    STATE.jobs[job.id] = job
    STATE.active_job = job.id
    job.start()
    return {"jobId": job.id}


def h_job(body):
    job = STATE.jobs.get(body.get("jobId"))
    if not job:
        raise ValueError("Unknown job.")
    snap = job.snapshot(int(body.get("since") or 0))
    if body.get("withTable") or job.state != "running":
        headers, rows = job.table()
        snap["table"] = {"headers": headers, "rows": rows}
    return snap


def h_stop(body):
    job = STATE.jobs.get(body.get("jobId"))
    if job:
        job.stop()
    return {"ok": True}


def h_retry(body):
    """Create a new dataset from rows that FAILED / NOT RUN / CHECK in a job."""
    job = STATE.jobs.get(body.get("jobId"))
    if not job:
        raise ValueError("Unknown job.")
    want = set(body.get("results") or ["FAILED", "NOT RUN"])
    keep = {r["row"] for r in job.result_rows() if r["result"] in want}
    rows = [r for r in job.dataset["rows"] if r["row"] in keep]
    if not rows:
        raise ValueError("No rows to retry.")
    ds = dict(job.dataset)
    ds["rows"] = rows
    ds["summary"] = {"total": len(rows), "ok": sum(1 for r in rows if r["status"] == "ok"),
                     "warning": sum(1 for r in rows if r["status"] == "warning"),
                     "error": sum(1 for r in rows if r["status"] == "error")}
    ds["summary"]["valid"] = ds["summary"]["ok"] + ds["summary"]["warning"]
    ds_id = uuid.uuid4().hex[:12]
    STATE.datasets[ds_id] = ds
    return {"datasetId": ds_id, "filename": "retry of " + job.dataset.get("filename", ""), **_public_dataset(ds)}


def h_settings(body):
    return {"settings": _settings(body)}


POST_ROUTES = {
    "/api/meta": h_meta, "/api/status": h_status, "/api/connect": h_connect,
    "/api/disconnect": h_disconnect, "/api/test": h_test, "/api/parse": h_parse,
    "/api/dryrun": h_dryrun, "/api/submit": h_submit, "/api/job": h_job, "/api/stop": h_stop,
    "/api/retry": h_retry, "/api/settings": h_settings,
}


# --------------------------------------------------------------------------- HTTP server

class Handler(BaseHTTPRequestHandler):
    server_version = "FortiCloudBulkManager"

    def log_message(self, fmt, *args):
        pass

    def _host_ok(self):
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("127.0.0.1", "localhost")

    def _send(self, status, body, ctype="application/json", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, default=str).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, {"error": "Forbidden host"})
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        if u.path in ("/", "/index.html"):
            with open(os.path.join(HERE, "static", "index.html"), encoding="utf-8") as f:
                html = f.read().replace("__SESSION_KEY__", STATE.session_key)
            return self._send(200, html, "text/html; charset=utf-8",
                              {"Content-Security-Policy": "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                                                          "script-src 'self' 'unsafe-inline'; img-src 'self' data:"})
        if u.path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return
        if q.get("s", [""])[0] != STATE.session_key:
            return self._send(403, {"error": "Missing session key"})
        if u.path == "/download/template":
            return self._template(q.get("op", [""])[0], q.get("fmt", ["csv"])[0])
        if u.path == "/download/results":
            return self._results(q.get("job", [""])[0], q.get("fmt", ["csv"])[0])
        self._send(404, {"error": "Not found"})

    def _template(self, op_key, fmt):
        if op_key not in ops.OPERATIONS:
            return self._send(404, {"error": "Unknown operation"})
        headers, help_rows = ops.template_rows(op_key)
        name = "forticloud_%s_template" % op_key
        if fmt == "xlsx":
            data = write_xlsx([(ops.OPERATIONS[op_key]["short"], [headers]), ("Instructions", help_rows)])
            return self._send(200, data, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                              {"Content-Disposition": 'attachment; filename="%s.xlsx"' % name})
        return self._send(200, write_csv(headers, []), "text/csv; charset=utf-8",
                          {"Content-Disposition": 'attachment; filename="%s.csv"' % name})

    def _results(self, job_id, fmt):
        job = STATE.jobs.get(job_id)
        if not job:
            return self._send(404, {"error": "Unknown job"})
        headers, rows = job.table()
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(job.started))
        name = "%s_%s_results" % (stamp, job.op_key)
        if fmt == "xlsx":
            data = write_xlsx([("Results", [headers] + rows)])
            return self._send(200, data, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                              {"Content-Disposition": 'attachment; filename="%s.xlsx"' % name})
        return self._send(200, write_csv(headers, rows), "text/csv; charset=utf-8",
                          {"Content-Disposition": 'attachment; filename="%s.csv"' % name})

    def do_POST(self):
        if not self._host_ok():
            return self._send(403, {"error": "Forbidden host"})
        if self.headers.get("X-Session") != STATE.session_key:
            return self._send(403, {"error": "Invalid session. Reload the page."})
        handler = POST_ROUTES.get(urllib.parse.urlparse(self.path).path)
        if not handler:
            return self._send(404, {"error": "Not found"})
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_UPLOAD * 2:
            return self._send(413, {"error": "Request too large"})
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
            self._send(200, handler(body))
        except (ValueError, ApiError) as e:
            self._send(400, {"error": str(e)})
        except Exception as e:  # pragma: no cover
            self._send(500, {"error": "Internal error: %s" % e})


def main():
    global STATE
    p = argparse.ArgumentParser(description="FortiCloud Asset Bulk Manager")
    p.add_argument("--port", type=int, default=8765, help="local port (default 8765)")
    p.add_argument("--demo", action="store_true", help="use the built-in fake FortiCloud (no real API calls)")
    p.add_argument("--no-browser", action="store_true", help="do not open the browser automatically")
    p.add_argument("--auth-url", default=os.environ.get("FCM_AUTH_URL"), help=argparse.SUPPRESS)
    p.add_argument("--api-base", default=os.environ.get("FCM_API_BASE"), help=argparse.SUPPRESS)
    args = p.parse_args()

    auth_url, api_base = args.auth_url, args.api_base
    results_dir = None
    if args.demo:
        from fcm import mock
        msrv = mock.start(0)
        mport = msrv.server_address[1]
        auth_url = "http://127.0.0.1:%d/api/v1/oauth/token/" % mport
        api_base = "http://127.0.0.1:%d/ES/api/registration/v3" % mport
        results_dir = os.path.join(HERE, "results", "demo")
    STATE = AppState(args.demo, auth_url, api_base, results_dir)

    srv = None
    for port in range(args.port, args.port + 20):
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            continue
    if srv is None:
        sys.exit("No free port found between %d and %d." % (args.port, args.port + 19))
    url = "http://127.0.0.1:%d/" % srv.server_address[1]
    print("=" * 64)
    print(" FortiCloud Asset Bulk Manager %s%s" % (__version__, "  [DEMO MODE]" if args.demo else ""))
    print(" Open: %s" % url)
    print(" Results folder: %s" % STATE.results_dir)
    print(" Press Ctrl+C to quit (the OAuth token is revoked on exit).")
    print("=" * 64)
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        if STATE.client:
            STATE.client.revoke()
        srv.server_close()


if __name__ == "__main__":
    main()

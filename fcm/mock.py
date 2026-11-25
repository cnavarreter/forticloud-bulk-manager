"""A small fake FortiCloud (IAM token + Asset Management V3) server for demo / practice runs.

Author: Cesar Navarrete <cenavar@gmail.com>

Nothing here talks to Fortinet. Behaviour:
  * Any API ID/password works except password "bad" (returns an auth error).
  * Serial numbers containing "BAD" are rejected; serials containing "SLOW" take 3s.
  * The first call carrying a serial containing "RATE" gets HTTP 429 once.
"""

import json
import random
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STATE = {"tokens": set(), "assets": {}, "rate_hit": set(), "lock": threading.Lock()}
BASE = "/ES/api/registration/v3"
MODELS = ["FortiGate 60F", "FortiGate 100F", "FortiSwitch 124F", "FortiAP 231F", "FortiGate VM 2 CPU"]


def _model_for(serial):
    if serial.startswith("FGT6"):
        return "FortiGate 60F"
    if serial.startswith("FGT1"):
        return "FortiGate 100F"
    if serial.startswith("FS"):
        return "FortiSwitch 124F"
    if serial.startswith("FP"):
        return "FortiAP 231F"
    return random.choice(MODELS)


def _ok(extra=None):
    body = {"status": 0, "message": "", "version": "3.0", "build": "mock", "token": "mock-token", "error": ""}
    body.update(extra or {})
    return 200, body


def _fail(msg, code=-1):
    return 200, {"status": code, "message": msg, "version": "3.0", "build": "mock", "error": msg}


class Handler(BaseHTTPRequestHandler):
    server_version = "MockFortiCloud/1.0"

    def log_message(self, *a):
        pass

    def _send(self, status, body, headers=None):
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self._send(400, {"error": "invalid json"})
        path = self.path.split("?")[0]

        if path == "/api/v1/oauth/token/":
            if body.get("password") == "bad" or not body.get("username"):
                return self._send(401, {"error": "invalid_grant", "error_description": "Invalid credentials given."})
            tok = secrets.token_urlsafe(20)
            STATE["tokens"].add(tok)
            return self._send(200, {"access_token": tok, "expires_in": 14400, "token_type": "Bearer",
                                    "scope": "read write", "refresh_token": secrets.token_urlsafe(20),
                                    "message": "successfully authenticated", "status": "success"})
        if path == "/api/v1/oauth/revoke_token/":
            STATE["tokens"].discard(body.get("token"))
            return self._send(200, {"status": "success", "message": "Access token revoked"})

        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or auth[7:] not in STATE["tokens"]:
            return self._send(401, {"status": -1, "message": "Invalid or expired token"})
        if not path.startswith(BASE):
            return self._send(404, {"status": -1, "message": "Not found"})
        op = path[len(BASE):]

        serials = []
        if "serialNumber" in body:
            serials = [body["serialNumber"]]
        serials += body.get("serialNumbers") or []
        serials += [u.get("serialNumber", "") for u in body.get("registrationUnits") or []]
        for s in serials:
            if "RATE" in s and s not in STATE["rate_hit"]:
                STATE["rate_hit"].add(s)
                return self._send(429, {"status": -1, "message": "Too many requests"}, {"Retry-After": "2"})
            if "SLOW" in s:
                time.sleep(3)
        bad = [s for s in serials if "BAD" in s]

        status, resp = self._route(op, body, serials, bad)
        self._send(status, resp)

    def _route(self, op, body, serials, bad):
        A = STATE["assets"]
        if op == "/products/list":
            return _ok({"assets": []})
        if op == "/products/register":
            if bad:
                return _fail("Serial number %s is invalid or not eligible for registration." % bad[0])
            for u in body.get("registrationUnits") or []:
                s = u["serialNumber"]
                if s in A and not A[s].get("isDecommissioned"):
                    return _fail("Serial number %s has already been registered." % s)
            assets = []
            for u in body["registrationUnits"]:
                s = u["serialNumber"]
                loc = None
                ref = (u.get("location") or {}).get("ref")
                if ref:
                    loc = (body.get("locations") or [])[int(ref.rsplit("/", 1)[1])]
                A[s] = {"serialNumber": s, "productModel": _model_for(s), "description": u.get("description", ""),
                        "folderId": u.get("folderId"), "folderPath": "/My Assets" if not u.get("folderId") else "/Folder %s" % u["folderId"],
                        "registrationDate": time.strftime("%Y-%m-%dT%H:%M:%S"), "isDecommissioned": False,
                        "accountId": body.get("accountId") or 854651, "location": loc,
                        "contracts": [u["contractNumber"]] if u.get("contractNumber") else []}
                assets.append({"serialNumber": s, "productModel": A[s]["productModel"], "status": "Registered",
                               "registrationDate": A[s]["registrationDate"], "accountId": A[s]["accountId"],
                               "description": A[s]["description"], "isDecommissioned": False,
                               "entitlements": [], "assetGroups": [], "warrantySupports": []})
            return _ok({"assets": assets})
        if op == "/products/details":
            s = serials[0] if serials else ""
            if bad or (s not in A and not s.startswith("F")):
                return _fail("Serial number %s not found in this account." % s)
            a = A.get(s) or {"serialNumber": s, "productModel": _model_for(s), "description": "",
                             "folderPath": "/My Assets", "registrationDate": "2024-03-14T10:00:00",
                             "isDecommissioned": False, "accountId": 854651, "contracts": []}
            ents = [] if a.get("isDecommissioned") else [
                {"level": 20, "levelDesc": "Premium", "type": 11, "typeDesc": "Firmware & General Updates",
                 "startDate": "2025-01-01T00:00:00", "endDate": "2027-01-01T00:00:00"},
                {"level": 20, "levelDesc": "Premium", "type": 12, "typeDesc": "Enhanced Support",
                 "startDate": "2025-01-01T00:00:00", "endDate": "2027-01-01T00:00:00"}]
            return _ok({"assetDetails": {
                "productModel": a["productModel"], "productModelEoR": "", "productModelEoS": "",
                "serialNumber": s, "accountId": a["accountId"], "isDecommissioned": a["isDecommissioned"],
                "registrationDate": a["registrationDate"], "assetGroups": [],
                "contracts": [{"contractNumber": c} for c in a.get("contracts", [])],
                "folderId": a.get("folderId"), "folderPath": a.get("folderPath"), "description": a.get("description"),
                "partner": "Demo Partner Inc.", "license": [], "entitlements": ents, "location": a.get("location"),
                "warrantySupports": [], "warrantyTypeName": "Bundle", "licenseKeys": []}})
        if bad:
            return _fail("Serial number %s not found in this account." % bad[0])
        if op == "/products/description":
            A.setdefault(serials[0], {}).update({"description": body.get("description", "")})
            return _ok()
        if op == "/products/location":
            A.setdefault(serials[0], {}).update({"location": body.get("location")})
            return _ok()
        if op == "/products/folder":
            for s in serials:
                A.setdefault(s, {}).update({"folderId": body.get("folderId"),
                                            "folderPath": "/My Assets" if body.get("folderId") is None else "/Folder %s" % body["folderId"]})
            return _ok()
        if op == "/products/decommission":
            for s in serials:
                A.setdefault(s, {}).update({"isDecommissioned": True})
            return _ok()
        if op == "/products/transfer":
            return _ok({"assets": [{"serialNumber": s, "accountId": body.get("targetAccountId")} for s in serials]})
        return 404, {"status": -1, "message": "Unknown endpoint %s" % op}


def start(port=0):
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


if __name__ == "__main__":
    s = start(8899)
    print("Mock FortiCloud running on http://127.0.0.1:8899 (Ctrl+C to stop)")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass

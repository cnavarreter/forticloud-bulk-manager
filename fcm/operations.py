"""Operation definitions, row validation, request building and response interpretation.

Based on the Fortinet Asset Management API V3 (Product) specification.
"""

import json
import re

# ISO 3166-1 alpha-2 codes
ISO_COUNTRIES = set("""
AD AE AF AG AI AL AM AO AQ AR AS AT AU AW AX AZ BA BB BD BE BF BG BH BI BJ BL BM BN BO BQ BR BS
BT BV BW BY BZ CA CC CD CF CG CH CI CK CL CM CN CO CR CU CV CW CX CY CZ DE DJ DK DM DO DZ EC EE
EG EH ER ES ET FI FJ FK FM FO FR GA GB GD GE GF GG GH GI GL GM GN GP GQ GR GS GT GU GW GY HK HM
HN HR HT HU ID IE IL IM IN IO IQ IR IS IT JE JM JO JP KE KG KH KI KM KN KP KR KW KY KZ LA LB LC
LI LK LR LS LT LU LV LY MA MC MD ME MF MG MH MK ML MM MN MO MP MQ MR MS MT MU MV MW MX MY MZ NA
NC NE NF NG NI NL NO NP NR NU NZ OM PA PE PF PG PH PK PL PM PN PR PS PT PW PY QA RE RO RS RU RW
SA SB SC SD SE SG SH SI SJ SK SL SM SN SO SR SS ST SV SX SY SZ TC TD TF TG TH TJ TK TL TM TN TO
TR TT TV TW TZ UA UG UM US UY UZ VA VC VE VG VI VN VU WF WS YE YT ZA ZM ZW XK
""".split())
STATE_CODE_COUNTRIES = {"US", "CA", "IN", "BR", "UA"}

PHONE_RE = re.compile(r"^\+\d{1,4} \d{4,15}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
SERIAL_RE = re.compile(r"^[A-Z0-9]{8,20}$")

LOCATION_FIELDS = ["company", "address", "city", "stateOrProvince", "countryCode",
                   "postalCode", "email", "phone", "fax"]


def col(name, required=False, help="", example="", aliases=(), kind="text"):
    return {"name": name, "required": required, "help": help, "example": example,
            "aliases": list(aliases), "kind": kind}


def _location_cols(prefix_help=""):
    return [
        col("company", help=prefix_help + "Company name", example="Test Company Inc", aliases=["companyname", "locationcompany"]),
        col("address", help="Street address", example="1234 Wall Street", aliases=["street", "streetaddress"]),
        col("city", help="City", example="Sunnyvale"),
        col("stateOrProvince", help="2-letter code required for US, CA, IN, BR, UA", example="CA", aliases=["state", "province", "region"]),
        col("countryCode", help="ISO 3166 alpha-2 (US, GB, MX...). Required if any location field is used", example="US", aliases=["country"]),
        col("postalCode", help="ZIP / postal code", example="94086", aliases=["zip", "zipcode", "postcode"]),
        col("email", help="Contact email", example="noc@testcompany.com", kind="email"),
        col("phone", help='Format "+<country code> <number>", e.g. +1 4085551234', example="+1 4085551234", kind="phone"),
        col("fax", help='Same format as phone', example="", kind="phone"),
    ]


SERIAL_COL = col("serialNumber", required=True, help="Product serial number", example="FGT60FTK21000001",
                 aliases=["serial", "sn", "serialno", "serialnum", "serial#"])

OPERATIONS = {
    "register": {
        "title": "Register products & contracts",
        "short": "Register",
        "endpoint": "/products/register",
        "permission": "ReadWrite / Admin",
        "write": True,
        "confirmWord": "REGISTER",
        "description": "Registers products to your account and optionally registers a contract (support/"
                       "service registration code) against each product. Sent in batches of up to 10 units, "
                       "the documented maximum per registration call.",
        "columns": [
            SERIAL_COL,
            col("contractNumber", help="Contract / registration code to register with this product",
                example="2121DJ8902", aliases=["contract", "contractno", "registrationcode", "contractcode"]),
            col("description", help="Product description", example="Branch 12 primary", aliases=["desc"]),
            col("folderId", help="Asset folder ID (blank = My Assets)", example="", aliases=["folder"], kind="int"),
            col("assetGroupIds", help="Comma-separated asset group IDs, e.g. 6,12", example="",
                aliases=["assetgroups", "assetgroupid", "groups"], kind="intlist"),
            col("additionalInfo", help="Extra info some products need (system ID, IP address...)", example="",
                aliases=["info", "additional"]),
            col("cloudKey", help="Cloud key, if the product uses one", example="", aliases=["cloudkey"]),
            col("isGovernment", help="yes / no - whether the product is used by a government entity", example="no",
                aliases=["government", "gov"], kind="bool"),
        ] + _location_cols(),
    },
    "details": {
        "title": "Look up product details",
        "short": "Look up",
        "endpoint": "/products/details",
        "permission": "Read",
        "write": False,
        "confirmWord": None,
        "description": "Read-only. Returns model, registration date, folder, description, support coverage and "
                       "licenses for each serial number - useful to verify a registration run.",
        "columns": [SERIAL_COL],
    },
    "description": {
        "title": "Update descriptions",
        "short": "Description",
        "endpoint": "/products/description",
        "permission": "ReadWrite / Admin",
        "write": True,
        "confirmWord": None,
        "description": "Sets the description of each product. One API call per row. A blank description "
                       "clears the existing one.",
        "columns": [SERIAL_COL, col("description", help="New description (blank clears it)",
                                    example="Branch 12 primary", aliases=["desc", "newdescription"])],
    },
    "location": {
        "title": "Update locations",
        "short": "Location",
        "endpoint": "/products/location",
        "permission": "ReadWrite / Admin",
        "write": True,
        "confirmWord": None,
        "description": "Sets (or clears) the location of each product. One API call per row. Set clearLocation "
                       "to yes to delete the location.",
        "columns": [SERIAL_COL] + _location_cols() + [
            col("clearLocation", help="yes = delete the product's location (other location columns ignored)",
                example="", aliases=["clear", "deletelocation", "removelocation"], kind="bool")],
    },
    "folder": {
        "title": "Move to asset folder",
        "short": "Move folder",
        "endpoint": "/products/folder",
        "permission": "ReadWrite / Admin",
        "write": True,
        "confirmWord": None,
        "description": "Moves products into an asset folder. Rows are grouped by folderId and sent in batches. "
                       "Leave folderId blank to move back to My Assets.",
        "columns": [SERIAL_COL, col("folderId", help="Target asset folder ID (blank = My Assets)",
                                    example="1551555", aliases=["folder", "targetfolderid", "targetfolder"], kind="int")],
    },
    "decommission": {
        "title": "Decommission products",
        "short": "Decommission",
        "endpoint": "/products/decommission",
        "permission": "ReadWrite / Admin",
        "write": True,
        "confirmWord": "DECOMMISSION",
        "description": "Marks products as decommissioned. Treat this as permanent: reversing it may require "
                       "Fortinet Customer Service.",
        "columns": [SERIAL_COL],
    },
    "transfer": {
        "title": "Transfer between accounts",
        "short": "Transfer",
        "endpoint": "/products/transfer",
        "permission": "ReadWrite / Admin + OU scope",
        "write": True,
        "confirmWord": "TRANSFER",
        "description": "Moves products between accounts in the same Organization. The API user needs access to "
                       "BOTH accounts through an Organization (OU) scope - a Local-scope API user will be "
                       "rejected by FortiCloud.",
        "columns": [SERIAL_COL,
                    col("sourceAccountId", required=True, help="Account the product is in now", example="8844522",
                        aliases=["source", "sourceaccount", "fromaccount", "fromaccountid"], kind="int"),
                    col("targetAccountId", required=True, help="Account to move it to", example="123222",
                        aliases=["target", "targetaccount", "toaccount", "toaccountid"], kind="int")],
    },
}

ORDER = ["register", "details", "description", "location", "folder", "decommission", "transfer"]


def public_definitions():
    out = []
    for key in ORDER:
        op = OPERATIONS[key]
        out.append({"key": key, **{k: v for k, v in op.items()}})
    return out


# --------------------------------------------------------------------------- parsing helpers

def _norm(s):
    return re.sub(r"[^a-z0-9#]", "", (s or "").lower())


def map_headers(op_key, headers):
    """Return (mapping index->column name, unknown header list, missing required list)."""
    cols = OPERATIONS[op_key]["columns"]
    lookup = {}
    for c in cols:
        lookup[_norm(c["name"])] = c["name"]
        for a in c["aliases"]:
            lookup[_norm(a)] = c["name"]
    mapping, unknown, seen = {}, [], set()
    for i, h in enumerate(headers):
        name = lookup.get(_norm(h))
        if name and name not in seen:
            mapping[i] = name
            seen.add(name)
        elif h.strip():
            unknown.append(h)
    missing = [c["name"] for c in cols if c["required"] and c["name"] not in seen]
    return mapping, unknown, missing


def _parse_bool(v):
    s = v.strip().lower()
    if s in ("yes", "y", "true", "1", "x"):
        return True
    if s in ("no", "n", "false", "0"):
        return False
    raise ValueError


def _parse_int(v):
    s = v.strip().replace(",", "")
    if re.fullmatch(r"\d+(\.0+)?", s):
        return int(float(s))
    raise ValueError


def _validate_location(data, errors, warnings, required=False):
    loc = {f: data.get(f, "").strip() for f in LOCATION_FIELDS}
    if not any(loc.values()):
        if required:
            errors.append("Location fields are empty (set clearLocation=yes to delete a location).")
        return None
    cc = loc["countryCode"].upper()
    loc["countryCode"] = cc
    if not cc:
        errors.append("countryCode is required when location fields are used.")
    elif cc not in ISO_COUNTRIES:
        errors.append("countryCode '%s' is not a valid ISO 3166 alpha-2 code." % cc)
    st = loc["stateOrProvince"]
    if cc in STATE_CODE_COUNTRIES:
        if not re.fullmatch(r"[A-Za-z]{2}", st or ""):
            errors.append("stateOrProvince must be a 2-letter code for %s (got '%s')." % (cc, st))
        else:
            loc["stateOrProvince"] = st.upper()
    for f in ("phone", "fax"):
        if loc[f] and not PHONE_RE.match(loc[f]):
            errors.append('%s "%s" must look like "+1 4085551234" (plus, country code, one space, digits).'
                          % (f, loc[f]))
    if loc["email"] and not EMAIL_RE.match(loc["email"]):
        errors.append("email '%s' does not look valid." % loc["email"])
    for f in ("company", "address", "city"):
        if not loc[f]:
            warnings.append("Location has no %s." % f)
    return {k: v for k, v in loc.items() if v}


def validate(op_key, headers, raw_rows):
    """Validate rows. Returns dict with per-row results and summary."""
    op = OPERATIONS[op_key]
    mapping, unknown, missing = map_headers(op_key, headers)
    kinds = {c["name"]: c["kind"] for c in op["columns"]}
    results = []
    seen_serials = {}
    for idx, raw in enumerate(raw_rows):
        data = {name: (raw[i] if i < len(raw) else "").strip() for i, name in mapping.items()}
        errors, warnings, clean = [], [], {}
        serial = re.sub(r"\s+", "", data.get("serialNumber", "")).upper()
        if not serial:
            errors.append("serialNumber is empty.")
        else:
            clean["serialNumber"] = serial
            if not SERIAL_RE.match(serial):
                warnings.append("Serial '%s' has an unusual format - double-check it." % serial)
            if serial in seen_serials:
                errors.append("Duplicate serial (also on row %d)." % (seen_serials[serial] + 2))
            else:
                seen_serials[serial] = idx

        for name, kind in kinds.items():
            if name == "serialNumber" or name in LOCATION_FIELDS or name not in data:
                continue
            val = data[name]
            if val == "":
                if name in ("sourceAccountId", "targetAccountId"):
                    errors.append("%s is required." % name)
                continue
            try:
                if kind == "int":
                    clean[name] = _parse_int(val)
                elif kind == "bool":
                    clean[name] = _parse_bool(val)
                elif kind == "intlist":
                    parts = [p for p in re.split(r"[,\s;\[\]]+", val) if p]
                    clean[name] = "[" + ",".join(str(_parse_int(p)) for p in parts) + "]"
                else:
                    clean[name] = val
            except ValueError:
                expected = {"int": "a whole number", "bool": "yes or no",
                            "intlist": "comma-separated numbers"}.get(kind, "valid text")
                errors.append("%s '%s' should be %s." % (name, val, expected))

        if op_key == "description" and "description" in data:
            clean["description"] = data.get("description", "")
        if op_key == "transfer":
            if clean.get("sourceAccountId") and clean.get("sourceAccountId") == clean.get("targetAccountId"):
                errors.append("sourceAccountId and targetAccountId are the same.")

        if op_key == "register":
            loc = _validate_location(data, errors, warnings)
            if loc:
                clean["_location"] = loc
        elif op_key == "location":
            if clean.get("clearLocation"):
                clean["_location"] = None
            else:
                clean["_location"] = _validate_location(data, errors, warnings, required=True)

        status = "error" if errors else ("warning" if warnings else "ok")
        results.append({"row": idx + 2, "raw": data, "clean": clean, "errors": errors,
                        "warnings": warnings, "status": status})

    if missing:
        for r in results:
            r["errors"].insert(0, "Missing required column(s): %s" % ", ".join(missing))
            r["status"] = "error"

    summary = {
        "total": len(results),
        "ok": sum(1 for r in results if r["status"] == "ok"),
        "warning": sum(1 for r in results if r["status"] == "warning"),
        "error": sum(1 for r in results if r["status"] == "error"),
    }
    summary["valid"] = summary["ok"] + summary["warning"]
    return {"columns": [mapping[i] for i in sorted(mapping)], "unknownColumns": unknown,
            "missingColumns": missing, "rows": results, "summary": summary}


# --------------------------------------------------------------------------- request building

def _chunks(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


def build_requests(op_key, rows, settings):
    """rows: validated row dicts (only valid ones are used). Returns list of request dicts."""
    op = OPERATIONS[op_key]
    valid = [r for r in rows if r["status"] != "error"]
    batch = max(1, int(settings.get("batchSize", 10)))
    account_id = settings.get("accountId")
    reqs = []

    if op_key == "register":
        batch = min(batch, 10)
        for chunk in _chunks(valid, batch):
            locations, loc_index, units = [], {}, []
            for r in chunk:
                c = r["clean"]
                unit = {"serialNumber": c["serialNumber"]}
                for f in ("contractNumber", "description", "folderId", "assetGroupIds",
                          "additionalInfo", "cloudKey", "isGovernment"):
                    if f in c:
                        unit[f] = c[f]
                if c.get("_location"):
                    key = json.dumps(c["_location"], sort_keys=True)
                    if key not in loc_index:
                        loc_index[key] = len(locations)
                        locations.append(c["_location"])
                    unit["location"] = {"ref": "#/locations/%d" % loc_index[key]}
                units.append(unit)
            payload = {"registrationUnits": units}
            if locations:
                payload["locations"] = locations
            if account_id:
                payload["accountId"] = int(account_id)
            reqs.append(_req(op, payload, chunk))

    elif op_key in ("details", "description", "location"):
        for r in valid:
            c = r["clean"]
            payload = {"serialNumber": c["serialNumber"]}
            if op_key == "description":
                payload["description"] = c.get("description", "")
            elif op_key == "location":
                payload["location"] = c.get("_location")
            reqs.append(_req(op, payload, [r]))

    elif op_key == "folder":
        groups = {}
        for r in valid:
            groups.setdefault(r["clean"].get("folderId"), []).append(r)
        for folder_id, grp in groups.items():
            for chunk in _chunks(grp, batch):
                reqs.append(_req(op, {"folderId": folder_id,
                                      "serialNumbers": [r["clean"]["serialNumber"] for r in chunk]}, chunk))

    elif op_key == "decommission":
        for chunk in _chunks(valid, batch):
            reqs.append(_req(op, {"serialNumbers": [r["clean"]["serialNumber"] for r in chunk]}, chunk))

    elif op_key == "transfer":
        groups = {}
        for r in valid:
            c = r["clean"]
            groups.setdefault((c["sourceAccountId"], c["targetAccountId"]), []).append(r)
        for (src, tgt), grp in groups.items():
            for chunk in _chunks(grp, batch):
                reqs.append(_req(op, {"sourceAccountId": src, "targetAccountId": tgt,
                                      "serialNumbers": [r["clean"]["serialNumber"] for r in chunk]}, chunk))
    return reqs


def _req(op, payload, rows):
    return {"path": op["endpoint"], "payload": payload,
            "rows": [r["row"] for r in rows],
            "serials": [r["clean"]["serialNumber"] for r in rows]}


# --------------------------------------------------------------------------- response interpretation

DETAIL_COLUMNS = ["productModel", "registrationDate", "accountId", "folderPath", "currentDescription",
                  "isDecommissioned", "supportCoverage", "latestSupportEnd", "contracts", "licenses",
                  "productModelEoS", "partner", "warrantyTypeName"]


def _summarize_details(body):
    ad = body.get("assetDetails") or {}
    ents = ad.get("entitlements") or []
    cov, ends = [], []
    for e in ents:
        if not isinstance(e, dict):
            continue
        end = (e.get("endDate") or "")[:10]
        if end:
            ends.append(end)
        label = e.get("typeDesc") or "Type %s" % e.get("type")
        if e.get("levelDesc"):
            label += " (%s)" % e["levelDesc"]
        if e.get("units"):
            label += " x%s" % e["units"]
        cov.append("%s until %s" % (label, end or "?"))
    contracts = []
    for c in ad.get("contracts") or []:
        if isinstance(c, dict):
            contracts.append(str(c.get("contractNumber") or c.get("number") or json.dumps(c)[:60]))
        else:
            contracts.append(str(c))
    lic = []
    for l in ad.get("license") or []:
        if isinstance(l, dict) and l.get("licenseNumber"):
            lic.append("%s (%s)" % (l["licenseNumber"], l.get("licenseSKU") or l.get("licenseType") or ""))
    return {
        "productModel": ad.get("productModel", ""),
        "registrationDate": (ad.get("registrationDate") or "")[:10],
        "accountId": ad.get("accountId", ""),
        "folderPath": ad.get("folderPath", ""),
        "currentDescription": ad.get("description", ""),
        "isDecommissioned": ad.get("isDecommissioned", ""),
        "supportCoverage": "; ".join(cov),
        "latestSupportEnd": max(ends) if ends else "",
        "contracts": "; ".join(contracts),
        "licenses": "; ".join(lic),
        "productModelEoS": (ad.get("productModelEoS") or "")[:10],
        "partner": ad.get("partner", ""),
        "warrantyTypeName": ad.get("warrantyTypeName", ""),
    }


def interpret(op_key, req, http_status, body, success, message):
    """Return {serial: {"result": ..., "message": ..., "extra": {...}}}."""
    out = {}
    if not success:
        msg = message or ("HTTP %s" % http_status)
        named = [s for s in req["serials"] if s in msg.upper()]
        for s in req["serials"]:
            if named and s not in named and len(req["serials"]) > 1:
                out[s] = {"result": "FAILED", "extra": {},
                          "message": "Not processed: FortiCloud rejected the whole request because of %s (%s). "
                                     "This row is probably fine - retry it." % (", ".join(named), msg)}
            else:
                out[s] = {"result": "FAILED", "message": msg, "extra": {}}
        return out

    if op_key == "register":
        returned = {}
        for a in (body.get("assets") or []) if isinstance(body, dict) else []:
            if isinstance(a, dict) and a.get("serialNumber"):
                returned[str(a["serialNumber"]).upper()] = a
        for s in req["serials"]:
            a = returned.get(s)
            if a is not None:
                extra = {"productModel": a.get("productModel", ""),
                         "registrationStatus": a.get("status", ""),
                         "registrationDate": (a.get("registrationDate") or "")[:10]}
                out[s] = {"result": "OK", "message": message or "Registered", "extra": extra}
            elif returned:
                out[s] = {"result": "CHECK", "message": "Call succeeded but this serial was not in the "
                          "response - verify with Look up details.", "extra": {}}
            else:
                out[s] = {"result": "OK", "message": message or "Accepted", "extra": {}}
    elif op_key == "details":
        s = req["serials"][0]
        out[s] = {"result": "OK", "message": "", "extra": _summarize_details(body)}
    else:
        done = {"description": "Description updated", "location": "Location updated",
                "folder": "Moved", "decommission": "Decommissioned", "transfer": "Transferred"}[op_key]
        for s in req["serials"]:
            out[s] = {"result": "OK", "message": message or done, "extra": {}}
    return out


def template_rows(op_key):
    cols = OPERATIONS[op_key]["columns"]
    headers = [c["name"] for c in cols]
    help_rows = [["Column", "Required", "Description", "Example"]] + [
        [c["name"], "yes" if c["required"] else "", c["help"], c["example"]] for c in cols]
    return headers, help_rows

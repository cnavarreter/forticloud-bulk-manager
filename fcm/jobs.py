"""Background job runner: sends prepared requests one by one and records per-serial results."""

import datetime
import json
import os
import threading
import time
import uuid

from . import operations as ops
from .client import ApiError, api_message, is_api_success, scrub
from .sheets import write_csv


class Job:
    def __init__(self, op_key, dataset, requests, settings, client, results_dir):
        self.id = uuid.uuid4().hex[:12]
        self.op_key = op_key
        self.dataset = dataset
        self.requests = requests
        self.settings = settings
        self.client = client
        self.results_dir = results_dir
        self.stop_event = threading.Event()
        self.state = "queued"
        self.reason = ""
        self.started = time.time()
        self.finished = None
        self.done_requests = 0
        self.waiting = ""
        self.log = []
        self.results = {}          # serial -> result dict
        self.saved_file = None
        self.lock = threading.Lock()
        self.thread = threading.Thread(target=self._run, daemon=True)

    # ------------------------------------------------------------------ helpers
    def _log(self, level, text, detail=None):
        with self.lock:
            self.log.append({"t": datetime.datetime.now().strftime("%H:%M:%S"), "level": level,
                             "text": text, "detail": detail})

    def _on_wait(self, seconds):
        self.waiting = "Pausing %.0fs to respect FortiCloud rate limits..." % seconds

    def start(self):
        self.state = "running"
        self.thread.start()

    def stop(self):
        self.stop_event.set()

    # ------------------------------------------------------------------ main loop
    def _run(self):
        op = ops.OPERATIONS[self.op_key]
        max_err = int(self.settings.get("maxErrorsPerHour", 8))
        stop_on_batch_error = bool(self.settings.get("stopOnError", False))
        self._log("info", "Starting %s: %d request(s) for %d serial(s)." % (
            op["title"], len(self.requests), sum(len(r["serials"]) for r in self.requests)))
        try:
            for i, req in enumerate(self.requests):
                if self.stop_event.is_set():
                    self.state, self.reason = "stopped", "Stopped by user."
                    break
                errs = self.client.limiter.stats()["errorsLastHour"]
                if max_err and errs >= max_err:
                    self.state = "stopped"
                    self.reason = ("Stopped automatically: %d API errors in the last hour. FortiCloud allows "
                                   "about 10 errors per hour per API user; continuing could lock the user out. "
                                   "Fix the failed rows and resume later." % errs)
                    self._log("error", self.reason)
                    break
                label = "Request %d/%d (%s, %d serial%s)" % (
                    i + 1, len(self.requests), req["path"], len(req["serials"]),
                    "" if len(req["serials"]) == 1 else "s")
                try:
                    status, body = self.client.call(req["path"], req["payload"], self.settings,
                                                    self.stop_event, self._on_wait, write_op=op["write"])
                    self.waiting = ""
                    success = is_api_success(status, body)
                    msg = api_message(body, status)
                    if not success and not msg:
                        msg = "HTTP %s" % status
                    outcome = ops.interpret(self.op_key, req, status, body, success, msg if not success else "")
                    self._log("ok" if success else "error",
                              "%s -> %s%s" % (label, "OK" if success else "FAILED",
                                              "" if success else ": " + msg),
                              {"httpStatus": status, "request": req["payload"],
                               "response": _trim(scrub(body))})
                except ApiError as e:
                    self.waiting = ""
                    if self.stop_event.is_set() and "Stopped by user" in str(e):
                        self.state, self.reason = "stopped", "Stopped by user."
                        break
                    outcome = {s: {"result": "FAILED", "message": str(e), "extra": {}} for s in req["serials"]}
                    self._log("error", "%s -> %s" % (label, e), {"request": req["payload"]})
                    success = False
                    if "Authentication failed" in str(e):
                        self.state, self.reason = "stopped", "Stopped: authentication failed. " + str(e)
                        with self.lock:
                            self.results.update({k: dict(v, batch=i + 1) for k, v in outcome.items()})
                        self.done_requests = i + 1
                        break
                with self.lock:
                    for k, v in outcome.items():
                        v["batch"] = i + 1
                        self.results[k] = v
                self.done_requests = i + 1
                if not success and stop_on_batch_error:
                    self.state, self.reason = "stopped", "Stopped after a failed request (Stop on first error is on)."
                    break
            else:
                self.state = "done"
        except Exception as e:  # unexpected
            self.state, self.reason = "error", "Unexpected error: %s" % e
            self._log("error", self.reason)
        if self.state == "running":
            self.state = "done"
        self.finished = time.time()
        self.waiting = ""
        counts = self.counts()
        self._log("info", "Finished (%s): %d OK, %d failed, %d to check, %d not run, %d skipped (invalid)." % (
            self.state, counts["OK"], counts["FAILED"], counts["CHECK"], counts["NOT RUN"], counts["SKIPPED"]))
        try:
            self.saved_file = self._save_results()
            self._log("info", "Results saved to %s" % self.saved_file)
        except Exception as e:
            self._log("error", "Could not save results file: %s" % e)

    # ------------------------------------------------------------------ results
    def result_rows(self):
        """One row per input row, original data + outcome."""
        out = []
        with self.lock:
            for r in self.dataset["rows"]:
                serial = r["clean"].get("serialNumber") or r["raw"].get("serialNumber", "")
                if r["status"] == "error":
                    res = {"result": "SKIPPED", "message": "; ".join(r["errors"]), "extra": {}}
                else:
                    res = self.results.get(serial) or {"result": "NOT RUN", "message": "", "extra": {}}
                out.append({"row": r["row"], "serialNumber": serial, "raw": r["raw"],
                            "result": res["result"], "message": res["message"],
                            "batch": res.get("batch", ""), "extra": res.get("extra", {})})
        return out

    def counts(self):
        c = {"OK": 0, "FAILED": 0, "CHECK": 0, "NOT RUN": 0, "SKIPPED": 0}
        for r in self.result_rows():
            c[r["result"]] = c.get(r["result"], 0) + 1
        return c

    def table(self):
        rows = self.result_rows()
        input_cols = self.dataset["columns"]
        extra_cols = ops.DETAIL_COLUMNS if self.op_key == "details" else []
        if self.op_key == "register":
            extra_cols = ["productModel", "registrationStatus", "registrationDate"]
        headers = ["row", "result", "message", "batch"] + input_cols + extra_cols
        body = []
        for r in rows:
            vals = [r["row"], r["result"], r["message"], r["batch"]]
            vals += [r["serialNumber"] if c == "serialNumber" else r["raw"].get(c, "") for c in input_cols]
            vals += [_s(r["extra"].get(c, "")) for c in extra_cols]
            body.append(vals)
        return headers, body

    def _save_results(self):
        os.makedirs(self.results_dir, exist_ok=True)
        stamp = datetime.datetime.fromtimestamp(self.started).strftime("%Y%m%d-%H%M%S")
        path = os.path.join(self.results_dir, "%s_%s_results.csv" % (stamp, self.op_key))
        headers, body = self.table()
        with open(path, "wb") as f:
            f.write(write_csv(headers, body))
        log_path = path.replace("_results.csv", "_log.json")
        with open(log_path, "w", encoding="utf-8") as f:
            json.dump({"operation": self.op_key, "state": self.state, "reason": self.reason,
                       "started": stamp, "log": self.log}, f, indent=2, default=str)
        return path

    def snapshot(self, since=0):
        with self.lock:
            log = self.log[since:]
        return {
            "id": self.id, "op": self.op_key, "state": self.state, "reason": self.reason,
            "doneRequests": self.done_requests, "totalRequests": len(self.requests),
            "waiting": self.waiting, "counts": self.counts(), "log": log, "logOffset": since + len(log),
            "elapsed": int((self.finished or time.time()) - self.started),
            "savedFile": self.saved_file, "limits": self.client.limiter.stats(),
        }


def _s(v):
    if isinstance(v, bool):
        return "yes" if v else "no"
    return "" if v is None else v


def _trim(body, limit=6000):
    try:
        txt = json.dumps(body)
    except Exception:
        return str(body)[:limit]
    if len(txt) <= limit:
        return body
    return txt[:limit] + "... (truncated)"

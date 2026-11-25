# FortiCloud Asset Bulk Manager

**Author:** Cesar Navarrete ([cenavar@gmail.com](mailto:cenavar@gmail.com))

A small local web app for bulk operations against the **Fortinet Asset Management API V3**
(`https://support.fortinet.com/ES/api/registration/v3`):

| Operation | Endpoint | What it does |
|---|---|---|
| Register | `/products/register` | Register products and (optionally) contracts, in batches of up to 10 |
| Look up | `/products/details` | Read model, registration, folder, coverage, contracts, licenses (read-only) |
| Description | `/products/description` | Set or clear product descriptions |
| Location | `/products/location` | Set or clear product locations |
| Move folder | `/products/folder` | Move products into an asset folder (or back to My Assets) |
| Decommission | `/products/decommission` | Decommission products (asks you to type DECOMMISSION) |
| Transfer | `/products/transfer` | Move products between accounts in the same Organization (needs OU scope) |

It runs entirely on your Mac and uses only the Python standard library, so there is nothing to install.
It listens on `127.0.0.1` only.

## Start it

1. Python 3.8+ is required. Check with `python3 --version` in Terminal.
   If it's missing, install it from python.org, or run `xcode-select --install`.
2. Double-click **`Start FortiCloud Bulk Manager.command`**.
   The first time, macOS may block it: right-click the file → **Open** → **Open**.
   If double-clicking does nothing, run `chmod +x *.command` once in Terminal from this folder.
3. Your browser opens at `http://127.0.0.1:8765/`. Keep the Terminal window open while you work.
   Press **Ctrl+C** in it to quit, which also revokes the OAuth token.

From Terminal you can also run `python3 app.py`, or `python3 app.py --port 9000`.

### Demo mode (practice / partner demos)

Double-click **`Start DEMO mode.command`** or run `python3 app.py --demo`. The app then talks to a fake
FortiCloud built into the app, so nothing is sent to Fortinet. Any credentials work. Serials containing `BAD` are
rejected, and a serial containing `RATE` triggers a one-time "429 Too Many Requests" so you can see the retry.

## Create the API user (one time)

1. In FortiCloud, go to **Services → IAM → Users → Add New → API User**.
2. Give it a permission profile that includes **Asset Management** with the access level you need:
   **Read** for Look up only, **ReadWrite** or **Admin** for everything else.
3. Download the credentials file. It contains the API ID, password and client IDs.
   In the app, click **Load IAM credentials file…** to fill the form from it. The file isn't saved anywhere.
4. The Client ID for this API is normally `assetmanagement`.

A Local-scope API user works on its own account, so leave **Account ID** blank.
**Transfer** only works for an API user with Organization (OU) scope that can access both accounts.

## Workflow

1. **Test connectivity.** This checks four things and shows a ✓/✗ for each:
   - DNS and TLS to the IAM server
   - DNS and TLS to the Asset Management API server
   - Getting an OAuth token
   - A read-only API call

   To also test-read a specific product, enter its serial in the box next to the button.
   A successful test leaves you connected.
2. **Pick an operation** and download its template (.csv or .xlsx).
   The .xlsx template has an *Instructions* sheet that explains each column.
3. **Fill one row per product** and drop the file into the app. You can also paste rows copied from Excel.
   Column names are matched loosely, so "Serial Number", "SN" and "serial" all work.
4. **Review the check results.** Rows with errors are skipped and never sent. The app checks for:
   - missing or duplicate serials
   - invalid country codes
   - non-2-letter state codes for US, CA, IN, BR and UA
   - phone or fax numbers not formatted like `+1 4085551234`
   - non-numeric folder or account IDs
5. **Dry run** shows the exact JSON requests without sending anything.
6. **Run** asks you to confirm and then shows live progress and an activity log.
   Click "details" next to a log line to see the request and response.
   Write operations ask for confirmation; register, decommission and transfer ask you to type a word.
7. **Results:** each row ends up as **OK**, **FAILED**, **CHECK**, **NOT RUN** or **SKIPPED**.
   - Download the results as CSV or XLSX. A copy is also saved automatically in `results/`,
     with a JSON log next to it.
   - **Load failed / not-run rows for retry** reloads just those rows. Fix the cause first.
   - Run **Look up** on the same serials to confirm what changed.

## FortiCloud limits and how the app handles them

Fortinet documents these limits for this API (per API user):
- 100 calls per minute
- 1000 calls per hour
- 10 errors per hour
- 10 units per registration call, with at most 5 errors per batch registration

The app's defaults stay under them. You can change them under **Run settings**.
- At most 90 calls per minute and 900 per hour, with at least 1 second between calls.
- Registration batches are capped at 10 units.
- The job **stops automatically after 8 API errors in a rolling hour**, so the API user doesn't get locked out.
  The counter is shown at the top right.
- HTTP 429 and 502/503/504 responses are retried (2 retries by default).
- Timeouts on write calls are *not* retried, because the change may already have applied.
  Those rows are marked FAILED with a note to verify them with Look up.
- If FortiCloud rejects a whole batch because of one serial, that serial is flagged.
  The other rows in the batch are marked "not processed - retry it".
- An API user can only have one active token at a time. Connecting from this app replaces the token
  any other script is using for the same API user.

## Security notes

- Credentials are held in memory only. They are never written to disk or logs, and tokens are stripped
  from logged responses.
- The server binds to 127.0.0.1. Every request must carry a random per-launch session key, and requests
  are checked against the `Host` header. This stops other websites in your browser from calling the app.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `CERTIFICATE_VERIFY_FAILED` | The app already trusts Python's CAs, certifi and your Mac's keychains (Apple roots + System keychain). If it still fails, your network probably inspects HTTPS: get the company root CA as a .pem from IT and start with `FCM_CA_BUNDLE=/path/ca.pem python3 app.py`. With python.org Python you can also run *Applications → Python 3.x → Install Certificates.command*. |
| DNS / connect step fails behind a proxy | Set `HTTPS_PROXY=http://proxy:port` before starting. The app uses it automatically. |
| Authentication failed (401) | Check the API ID, password and Client ID (`assetmanagement`), and that the permission profile includes Asset Management. |
| Read-only call returns 403 | The token works but the API user lacks Asset Management permission. |
| "Too many errors" or auto-stop | Wait for the hourly window to clear (see the counter at the top), fix the failed rows, then retry. |
| Port in use | The app tries the next 20 ports automatically, or you can pass `--port`. |

## Files

```
app.py                  local web server + API handlers
fcm/client.py           OAuth token, API calls, rate limiting, retries
fcm/operations.py       column definitions, validation, request building, result parsing
fcm/jobs.py             background job runner + results files
fcm/sheets.py           CSV/XLSX reader & writer (no external libraries)
fcm/mock.py             fake FortiCloud used by --demo
static/index.html       the web interface
results/                results CSV + JSON log from every run
```

## Author

Created and maintained by **Cesar Navarrete** — [cenavar@gmail.com](mailto:cenavar@gmail.com)

# Job Agent — Windows Apply Client

Runs on your Windows PC. It polls the Jetson's apply queue, opens each application in Chrome, and fills what it confidently can. Then it **stops**: you review the form, click the site's own Submit button, and press **Done** in the overlay.

Standalone package. It doesn't import anything from `app/` and has its own dependencies.

## What it will never do

- Click Submit, Send Application, Next/Continue, or any final-step control. Every click the client makes goes through a guard that refuses those.
- Fill SSN, date of birth, driver's license, passport, payment fields, e-signatures ("type your name to sign"), or anything labeled signature/consent/authorize/certify/attest. These are outlined red and listed as skipped.
- Answer EEO/demographic questions with real values. It picks "decline to self-identify" if that option exists; otherwise it leaves the question blank and flags it.
- Type passwords. If a sign-in page appears, it pauses and hands off to you.
- Solve or bypass CAPTCHAs. It hands off.
- Tick checkboxes, guess a dropdown option, or overwrite a value that's already filled in.
- Automate login.gov (USAJobs). Those requests are always handed to you.

Anything it isn't highly confident about is flagged (yellow) rather than guessed.

## Install

```powershell
cd job-agent
py -3.11 -m venv apply_client\.venv
apply_client\.venv\Scripts\pip install -r apply_client\requirements.txt
apply_client\.venv\Scripts\playwright install chrome
```

`playwright install chrome` sets up the real Chrome channel. If Chrome is already installed, it may report that and move on. If the Chrome channel is missing, the client falls back to Playwright's bundled Chromium with a warning.

## Configure

Copy `apply_client/.env.example` to `apply_client/.env`:

| Setting | Meaning |
|---------|---------|
| `JETSON_URL` | The dashboard, e.g. `http://192.168.5.58:8080` or your Tailscale name |
| `APPLY_CLIENT_TOKEN` | Must equal `APPLY_CLIENT_TOKEN` in the Jetson's `config/secrets.env` |
| `CHROME_PROFILE_DIR` | Persistent profile (default `%LOCALAPPDATA%\job-agent\chrome-profile`) |
| `DOWNLOAD_DIR` | Per-request PDF downloads, deleted afterwards (default `%LOCALAPPDATA%\job-agent\downloads`) |
| `POLL_INTERVAL_SECONDS` | Queue poll interval for `--watch` (default 20) |
| `HUMAN_TIMEOUT_MINUTES` | Report "abandoned" if you don't click Done/Abandon in time (default 25) |

### Log in to each ATS once

The client uses a persistent Chrome profile, so your sign-ins stick between runs. Before the first real run, open that profile and log in by hand to anything you apply through: LinkedIn, Indeed, and the Workday/Greenhouse accounts you use.

```powershell
apply_client\.venv\Scripts\python -m apply_client --dry-run --url https://www.linkedin.com/login
```

Log in in the window that opens, then press Enter in the terminal to close it. Repeat for each site. The client never types credentials. If a sign-in page shows up mid-application, it pauses and asks you.

## Run

```powershell
# Process the queue continuously (Ctrl+C to stop)
apply_client\.venv\Scripts\python -m apply_client --watch

# Process one request, then exit
apply_client\.venv\Scripts\python -m apply_client --queue-id 12

# Inspect a form: detection + what WOULD be filled, with outlines. Fills nothing, needs no Jetson.
apply_client\.venv\Scripts\python -m apply_client --dry-run --url https://boards.greenhouse.io/acme/jobs/123
```

Only one client can use the Chrome profile at a time. Stop `--watch` before running `--dry-run`, and close any regular Chrome window that is using that profile directory.

### What happens per request

1. Claims the request and downloads the tailored resume and cover letter PDFs.
2. Opens the job listing and clicks its Apply / Easy Apply button. That button only opens the application; it isn't a submit. A button inside a filled-out form is never clicked.
3. Detects the ATS (Greenhouse, Lever, Workday, Indeed, LinkedIn, iCIMS, …) and fills the page:
   - **High confidence:** filled silently (green outline).
   - **Medium confidence:** filled, but outlined yellow for you to check.
   - **Low confidence or unknown:** left blank and outlined yellow.
   - **Sensitive:** never touched, outlined red.
   - **Files:** uploads go last.
4. Shows the overlay bar: **Filled N · Needs you M** with **Rescan / Done / Abandon** buttons.
   - Done and Abandon need two clicks: the first arms the button, the second confirms.
   - Rescan fills the current step, which is useful for multi-step modals that don't change the URL (LinkedIn Easy Apply, some Indeed steps).
5. You click the site's own Next/Continue/Submit. On every new step or page, the client fills what's new, and the overlay counts accumulate.
6. When you click **Done**, it reports `completed`. The Jetson records the application and sends a Telegram message. **Abandon** reports `abandoned`, a timeout reports `abandoned`, and a crash reports `failed`. The tab is closed and the downloads are deleted in every case.

## Tests

```powershell
apply_client\.venv\Scripts\pip install -r apply_client\requirements-dev.txt
cd apply_client
.venv\Scripts\python -m pytest
```

- **Unit tests (no browser):** detector URL patterns, field matching tiers and never-fill precedence, option matching, and the API client (retries, a never-retried claim, error mapping).
- **Headless browser tests:** Greenhouse and Lever fixture forms, sensitive fields staying empty, the click guard refusing submit/next/sign-in controls, and native and custom dropdowns.
- **End-to-end runner test:** a fake Jetson, click-through from a listing page, the fill pass, the real two-click Done button, and cleanup.

Workday and Indeed have no fixture tests, because they need a real session. Use the manual protocol below.

## Manual test protocol

For each ATS, pick one real posting you're fine applying to, or one you'll abandon.

1. `--dry-run --url <application URL>`. Check the table: is each field's canonical and action right? Note anything wrong.
2. Queue it from the dashboard, then `--queue-id N`. Watch the terminal and the browser.
3. Before clicking Done or Abandon, record the results:

| ATS | Posting | Filled correctly | Flagged (right to flag?) | Wrong / missed | Notes |
|-----|---------|------------------|--------------------------|----------------|-------|
| Greenhouse | | | | | |
| Lever | | | | | |
| Workday | | | | | verify `data-automation-id`s in `ats/fillers/workday.py` |
| Indeed Easy Apply | | | | | |
| LinkedIn Easy Apply | | | | | use Rescan on each modal step |

Fix wrong mappings in `ats/synonyms.py` (label variants) or in the ATS's filler (`ats/fillers/*.py`), then run `--dry-run` again.

## Troubleshooting

- **"Chrome channel unavailable … falling back to bundled Chromium":** run `apply_client\.venv\Scripts\playwright install chrome`, or ignore it; Chromium works, but it uses its own profile.
- **"401 … Invalid or missing X-Apply-Token":** `APPLY_CLIENT_TOKEN` in `apply_client/.env` doesn't match the Jetson's `config/secrets.env`. Copy it exactly, with no quotes or spaces.
- **"503 … apply_client_token not configured":** the Jetson has no token set. Add `APPLY_CLIENT_TOKEN` to its `config/secrets.env` and restart the dashboard.
- **"Can't reach the Jetson":** check `JETSON_URL`, that the dashboard is running, and that the PC can reach it on the network or Tailscale.
- **"Couldn't find the apply button":** the listing layout changed or the job has expired. Click Apply yourself in the open tab, then press **Rescan**.
- **"Sign-in required" / "CAPTCHA":** do it yourself in the open tab, then press **Rescan**.
- **Chrome says the profile is in use:** another client, or a normal Chrome window, has that profile open. Close it.
- **A field was filled wrong:** run `--dry-run` on that page, find the field's canonical in the table, and adjust `ats/synonyms.py` or that ATS's filler.

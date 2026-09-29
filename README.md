# Job Search Agent

Autonomous job search agent built for the Jetson Orin Nano 8GB. Scrapes job boards, scores listings against your profile using a local LLM, and surfaces the best matches — all running 24/7 on a $200 board that sips 7-15W.

## Hardware

- **Board:** NVIDIA Jetson Orin Nano 8GB (shared CPU/GPU RAM)
- **Storage:** 1TB NVMe SSD
- **LLM:** Ollama running `llama3.2:3b-instruct-q4_K_M` (CPU-only, ~934MB)
- **Network:** WiFi, static IP `192.168.5.58`
- **Access:** SSH over LAN

## Quick Start

```bash
# Clone and set up (on the Nano)
cd ~/agent
python3 -m venv venv
source venv/bin/activate
pip install -e .
playwright install chromium

# Initialize the database
job-agent init-db

# Run a scrape
job-agent scrape --board indeed

# Evaluate scraped jobs with the local LLM
sudo systemctl start ollama
job-agent evaluate --model llama3.2:3b-instruct-q4_K_M

# View results
job-agent list
job-agent list --status evaluated    # top matches
job-agent list --status rejected     # see why they were rejected
job-agent list --id 5                # full detail for job #5
```

## Project Structure

```
~/agent/
├── app/
│   ├── cli.py                 # Typer CLI entry point
│   ├── config.py              # Pydantic config loader
│   ├── database.py            # Async SQLite with WAL mode
│   ├── logging_config.py      # Rotating file + console logging
│   ├── scrapers/
│   │   ├── base.py            # Abstract base scraper + AdaptiveHealth monitor
│   │   ├── indeed.py          # Indeed scraper (JSON-first parsing)
│   │   └── linkedin.py        # LinkedIn scraper (guest API + Playwright)
│   ├── evaluator/
│   │   ├── ollama_client.py   # Async Ollama HTTP client
│   │   └── pipeline.py        # LLM evaluation pipeline + auto-filtering
│   ├── dashboard/
│   │   ├── main.py            # FastAPI app + routes
│   │   ├── static/style.css   # Dark theme CSS
│   │   └── templates/         # Jinja2 + HTMX templates
│   └── scheduler/
│       └── runner.py          # APScheduler orchestration
├── config/
│   ├── profile.yaml           # Your job profile (SINGLE SOURCE OF TRUTH)
│   ├── boards.yaml            # Per-board scraper settings
│   └── secrets.env            # API keys (gitignored)
├── data/
│   ├── jobs.db                # SQLite database
│   └── html_snapshots/        # Raw HTML for parser debugging
├── logs/                      # Rotating app logs
├── tests/
│   └── test_core.py           # 47 stdlib tests
├── pyproject.toml             # Project metadata + dependencies
└── README.md                  # You are here
```

## Configuration

All shared config lives in `profile.yaml` — boards inherit from it automatically. Change your location, roles, or skills in one place and every scraper picks it up.

### profile.yaml (single source of truth)

Roles and skills are organized into **search lanes** — independent role families that are scraped and scored separately. `preferences`, `blacklist`, and `maintenance` are shared across all lanes.

```yaml
search_lanes:
  frontend_developer:
    enabled: true
    target_roles:
      - "Lead Frontend Developer"
      - "Senior Frontend Developer"
      - "Development Manager"
    target_field: "software/web development, engineering management"
    skills:
      must_have:       # LLM matches on ANY of these, not all
        - "JavaScript"
        - "React"
        - "TypeScript"
      nice_to_have:    # Bonus points
        - "Node.js"
        - "AEM"
    resume_version: "frontend_developer"

  marketing_manager:
    enabled: true
    target_roles:
      - "Marketing Manager"
      - "Digital Marketing Manager"
      - "Marketing Technology Manager"
    target_field: "digital marketing, marketing technology, web marketing operations"
    skills:
      must_have_any:   # Gate: job must list at least ONE of these, or score caps at 0.35
        - "AEM"
        - "Adobe Experience Manager"
        - "HTML"
        - "JavaScript"
      nice_to_have:
        - "Marketo"
        - "Adobe Analytics"
    resume_version: "marketing_manager"

preferences:
  location: "Homewood, IL"
  remote_ok: true
  hybrid_ok: true
  onsite_ok: false
  max_commute_miles: 40
  salary_min: 130000
  experience_years: 15

blacklist:
  companies:
    - "Company You Hate Inc."
  keywords:
    - "unpaid"
    - "intern"
    - "clearance required"
    - "junior"
    - "entry level"

maintenance:
  stale_listing_max_age_days: 30   # purge listings scraped longer ago than this
  preserve_statuses:               # ...unless they have one of these statuses
    - "approved"
    - "applied"
```

#### How lanes work

- **Scraping:** every scraper runs each enabled lane in turn, using that lane's `target_roles` (lowercased) as search queries. Each job is tagged with the lane that found it. If a second lane finds the same URL, the job is tagged `both`.
- **Evaluation:** a job is scored against its own lane's roles, field, and skills. A `both` job is scored once per lane, and its status is set by the **highest** lane score.
- **Dashboard:** the Jobs page has a lane filter row above the status tabs (a lane filter includes `both` jobs). Each job shows a lane badge, and `both` jobs show their per-lane evaluations side by side.
- Set `enabled: false` to pause a lane without deleting it. Jobs scraped before lanes existed are migrated to `frontend_developer`.

### boards.yaml (board-specific overrides only)

Boards inherit `location` and `radius_miles` from profile.yaml unless explicitly overridden. Search queries come from the search lanes (see above); a board that sets explicit `search_queries` (like `usajobs`, which uses federal job titles) runs those once per enabled lane instead.

```yaml
boards:
  indeed:
    enabled: true
    module: "app.scrapers.indeed"
    base_url: "https://www.indeed.com"
    max_pages: 3
    delay_min: 10.0
    delay_max: 30.0

  linkedin:
    enabled: true
    module: "app.scrapers.linkedin"
    base_url: "https://www.linkedin.com"
    max_pages: 3
    delay_min: 15.0        # LinkedIn is aggressive — stay slow
    delay_max: 40.0
```

## CLI Commands

| Command | Description |
|---------|-------------|
| `job-agent init-db` | Create/migrate the database |
| `job-agent scrape --board indeed` | Scrape Indeed for new listings |
| `job-agent scrape --board linkedin` | Scrape LinkedIn for new listings |
| `job-agent enrich` | Fetch full descriptions for jobs missing them |
| `job-agent evaluate --model llama3.2:3b-instruct-q4_K_M` | Score all new jobs with the local LLM |
| `job-agent evaluate --model llama3.2:3b-instruct-q4_K_M --id 5` | Evaluate a single job |
| `job-agent list` | Show all jobs in a table |
| `job-agent list --status evaluated` | Show only top matches |
| `job-agent list --status rejected` | Show rejected jobs with reasons |
| `job-agent list --id 5` | Full detail view for a job |
| `job-agent dashboard` | Start the web dashboard on port 8080 |
| `job-agent dashboard --port 3000` | Start on a custom port |
| `job-agent notify --test` | Send a test message to Telegram |
| `job-agent notify --digest` | Send the daily digest to Telegram |
| `job-agent polish --id 5 --lane frontend_developer` | Generate a cover letter for job #5 (alias: `cover-letter`) |
| `job-agent polish --id 5 --lane frontend_developer --force` | Regenerate, replacing a cached or edited letter |
| `job-agent migrate-cover-letters` | One-time: make pre-v2 cover letter drafts editable |
| `job-agent parse-linkedin` | Parse `resumes/linkedin_export.zip` into `resumes/linkedin_data.json` |
| `job-agent resume 5 --lane marketing_manager` | Generate a tailored resume (PDF + DOCX) for job #5 |
| `job-agent resume 5 --lane marketing_manager --force` | Regenerate, ignoring the cache |
| `job-agent status` | Database stats by status |

## Evaluation Scoring

The local LLM scores each job from 0.0 to 1.0 against your profile:

| Score | Status | Meaning |
|-------|--------|---------|
| >= 0.7 | `evaluated` | Ready for review — strong match |
| 0.4 - 0.7 | `maybe` | Worth a look, partial match |
| < 0.4 | `rejected` | Auto-rejected (reason stored in DB) |

Scoring rules: matching ANY must-have skill is a positive signal (not all required). Missing salary info is treated as neutral. Only clearly irrelevant jobs score below 0.3.

## Tailored Resumes

Generates a resume for a specific job and lane from your LinkedIn data, using Claude API. Every claim is checked against your own data. Skills or positions Claude can't back up are removed and listed as fabrication warnings.

### 1. Export your LinkedIn data

LinkedIn → Settings & Privacy → Data privacy → **Get a copy of your data**. Select at least Profile, Positions, Skills, Education, and Certifications. LinkedIn emails you a ZIP, anywhere from 10 minutes to a few hours later. Save it as `resumes/linkedin_export.zip`, then run:

```bash
job-agent parse-linkedin              # or: --zip path/to/export.zip
```

This writes `resumes/linkedin_data.json`. Re-run it whenever you update your LinkedIn profile. The ZIP, the parsed JSON, and `data/generated_resumes/` are all gitignored, because they contain your full work history and contact info.

### 2. Contact details (`config/profile.yaml`)

```yaml
contact:
  email: ""          # blank = use the primary email from the LinkedIn export
  phone: ""          # blank = use the phone from the LinkedIn export
  linkedin_url: ""   # not in the export — set it here
  portfolio_url: "https://mansa-tech.com/portfolio/"
```

### 3. Extra achievements (`resumes/additional_bullets.json`)

For metrics and projects that aren't on LinkedIn. Keys are company names exactly as they appear in your LinkedIn positions; matching is case-insensitive. Keys starting with `_` are ignored.

```json
{
  "Amazon Web Services (AWS)": [
    {"text": "Built AEM component library adopted across 12 product pages, cutting page build time 40%",
     "tags": ["AEM", "HTML", "JavaScript", "marketing"]}
  ]
}
```

These bullets are appended to the matching position before generation. Their `tags` count as evidence in the fabrication check, so only add real achievements.

### 4. Generate

```bash
job-agent resume 42 --lane marketing_manager
```

You can also generate from the dashboard: on a job page, click **Generate resume for this one**. A job tagged `both` offers one resume per lane. Output goes to `data/generated_resumes/{job_id}_{lane}_resume.pdf` / `.docx` / `.json`. A repeat request with the same job description, lane, and LinkedIn data is served from cache at no cost. Use `--force` or **Regenerate** to create a new one.

The resumes are ATS-friendly:
- Single column with no tables.
- Standard section order: Summary, Skills, Experience, Education, Certifications.
- Dates right-aligned on the same line as company and title.
- Real `•` bullets, Letter size with 0.75" margins.

## Cover Letters

Cover letters are written for one job **and** one search lane, from the best candidate data available. Every letter is saved as an editable text file.

### Setup

1. Get an API key at [console.anthropic.com](https://console.anthropic.com)
2. Add to `config/secrets.env`:

```env
ANTHROPIC_API_KEY=sk-ant-your-key-here
```

3. Run `job-agent parse-linkedin` (see [Tailored Resumes](#tailored-resumes)).

### Recommended order: resume first, then cover letter

```bash
job-agent resume 42 --lane marketing_manager
job-agent cover-letter --id 42 --lane marketing_manager   # alias of `polish`
```

The letter is written from the best candidate context it can find, in this order:

1. **Tailored resume** for this job + lane (`data/generated_resumes/{id}_{lane}_resume.json`). The letter matches the resume you're sending.
2. **LinkedIn data** (`resumes/linkedin_data.json` + `additional_bullets.json`). Works, but isn't aligned to this job's resume.
3. **Static resume PDF** in `resumes/` whose filename contains the lane's `resume_version`.
4. Nothing found → error pointing you at `job-agent parse-linkedin`.

The CLI prints which source was used. The dashboard shows a yellow warning when a letter wasn't written from the tailored resume.

### Template selection

Templates in `templates/` give structure and tone guidance to Claude; they aren't fill-in-the-blank:

| Lane | Job title | Template |
|------|-----------|----------|
| `marketing_manager` | any | `cover_letter_marketing.txt` |
| `frontend_developer` | contains lead / manager / director / head / principal / vp | `cover_letter_frontend_lead.txt` |
| `frontend_developer` | anything else | `cover_letter_frontend_ic.txt` |
| any other lane | any | `cover_letter_general.txt` |

### Editing

On a job's detail page, the cover letter panel is an editable text box:
- **Save edits** writes your text to `data/generated_cover_letters/{id}_{lane}_cover_letter.txt` and re-renders the PDF and DOCX.
- Download the letter as PDF, DOCX, or TXT.

Edited letters are marked `manual-edit` and are never overwritten by a normal generate. **Regenerate** asks for confirmation first. On the CLI, `--force` is required to replace an edited letter.

A repeat request with the same job description, lane, and candidate context is served from cache at no cost. Cost is roughly $0.01–0.03 per letter.

### Migrating old drafts

Before v2, cover letters were stored in the database (`evaluations.cover_letter_draft`). Those still show on the job page as a read-only "legacy draft". Run this once to turn them into editable files:

```bash
job-agent migrate-cover-letters
```

It's safe to re-run: a job + lane that already has a cover letter is skipped. Migrated drafts are kept like manual edits until you regenerate them.

## Adaptive Scraper Health

All scrapers include a built-in `AdaptiveHealth` monitor that evaluates after every page fetch and auto-adjusts behavior in real time. This handles the inevitable HTML structure changes and bot detection escalations that job boards love to throw at you.

### What it does

The monitor tracks block rates, parse error rates, and consecutive empty pages, then makes decisions:

| Condition | Action |
|-----------|--------|
| 30%+ fetch block rate | Increase delays by 1.5x |
| 60%+ fetch block rate | Playwright-first + delays at 2.5x |
| 2 consecutive blocks | Switch to Playwright-first for rest of run |
| 50%+ parse error rate | Switch to Playwright-first (httpx may be getting bot pages) |
| 3 consecutive empty pages (httpx) | Escalate to Playwright |
| 3 consecutive empty pages (Playwright) | Halt the run — HTML structure likely changed |

Every run's health metrics are persisted to the `scraper_health` table in SQLite, so you can track degradation over time:

```sql
SELECT source, run_at, pages_fetched, pages_blocked, parse_errors,
       fetch_strategy, delay_min_used, delay_max_used, notes
FROM scraper_health
ORDER BY run_at DESC
LIMIT 20;
```

### Tuning thresholds

The defaults work well, but if you need to adjust per-board, override in the scraper subclass:

```python
self.health.parse_error_escalate_pct = 0.40   # more sensitive
self.health.max_consecutive_empty = 2          # halt faster
self.health.max_delay_cap = 180.0              # allow longer waits
```

## Jetson Orin Nano Notes

The 8GB shared RAM is tight. Key setup decisions:

- **Desktop GUI disabled** to free ~1.5GB: `sudo systemctl set-default multi-user.target`
- **Max power mode**: `sudo nvpmodel -m 0 && sudo jetson_clocks`
- **CPU-only LLM inference**: Ollama service configured with `CUDA_VISIBLE_DEVICES=""` to avoid GPU memory contention
- **Model**: `llama3.2:3b-instruct-q4_K_M` (~934MB) — the largest model that fits comfortably
- **Static IP**: `192.168.5.58` configured via NetworkManager to prevent SSH disconnects
- **WiFi power save disabled**: `sudo iw wlan0 set power_save off`

### Ollama Service Config

```ini
# /etc/systemd/system/ollama.service
[Service]
Environment="CUDA_VISIBLE_DEVICES="
```

## Telegram Notifications

Get daily digests and scrape/eval summaries pushed to your phone.

### Setup

1. Create a bot via `@BotFather` in Telegram (send `/newbot`)
2. Open a chat with your bot and send it a message
3. Get your chat ID: `curl -s "https://api.telegram.org/bot<TOKEN>/getUpdates" | python3 -m json.tool`
4. Add credentials to `config/secrets.env`:

```env
TELEGRAM_BOT_TOKEN=your_bot_token_here
TELEGRAM_CHAT_ID=your_chat_id_here
```

5. Test it: `job-agent notify --test`

### What you'll receive

- **Daily digest at 8am** — stats, top matches, link to dashboard
- **Scrape summaries** — how many new jobs were found after each scrape
- **Evaluation summaries** — how many jobs scored review/maybe/rejected
- **Error alerts** — if a scraper or evaluation fails

## Web Dashboard

Phase 3 adds a web UI accessible from any device on your LAN (phone, laptop, etc.).

```bash
# Start the dashboard
source ~/agent/venv/bin/activate
job-agent dashboard

# Access from any device on your network:
# http://192.168.5.58:8080
```

Features: dark theme, mobile-responsive, HTMX-powered (no JS build step). Pages include a dashboard home with stats and top matches, a filterable job list with status tabs, and job detail pages with approve/reject/maybe buttons. You can also trigger scrape and evaluation runs directly from the dashboard.

### Running as a systemd service

```ini
# /etc/systemd/system/job-agent-dashboard.service
[Unit]
Description=Job Agent Dashboard
After=network.target ollama.service

[Service]
Type=simple
User=geechiedan68
WorkingDirectory=/home/geechiedan68/agent
ExecStart=/home/geechiedan68/agent/venv/bin/job-agent dashboard
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo cp job-agent-dashboard.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now job-agent-dashboard
```

## Scheduler (Automated Operation)

The built-in scheduler runs scraping and evaluation on autopilot:

- Weekday scrapes every 6 hours
- Nightly scrape + evaluate at 2:00 AM
- Eval catch-up at 3:30 AM
- Stale listing purge at 4:00 AM: deletes jobs scraped more than `maintenance.stale_listing_max_age_days` ago, unless their status is in `preserve_statuses` (default: approved, applied). Their evaluations, decisions, and applied records are deleted too. If a single run purges more than 100 jobs, you get a Telegram alert, since that usually means a config mistake.

Start with: `job-agent run-scheduler` (or set up as a systemd service)

## Re-running Evaluations

If you change the scoring prompt or profile, reset and re-evaluate:

```bash
source ~/agent/venv/bin/activate
sqlite3 ~/agent/data/jobs.db "DELETE FROM evaluations; UPDATE jobs SET status = 'new', rejection_reason = NULL;"
job-agent evaluate --model llama3.2:3b-instruct-q4_K_M
```

## Running Tests

```bash
source ~/agent/venv/bin/activate
python -m pytest tests/ -v
# or without pytest:
python tests/test_core.py
```

## Phase Status

- [x] **Phase 1** — Scaffolding, database, config system, Indeed scraper
- [x] **Phase 2** — Local LLM evaluation pipeline with auto-filtering
- [x] **Phase 3** — FastAPI + HTMX web dashboard + Telegram notifications
- [x] **Phase 4** — Cloud API cover letter polishing (Anthropic Claude, opt-in per job)
- [ ] **Phase 5** — Additional scrapers + hardening
  - [x] LinkedIn scraper (guest API + Playwright fallback)
  - [x] Adaptive scraper health monitor (auto-tuning delays, Playwright escalation, halt on structure changes)
  - [x] `scraper_health` DB table for historical metrics
  - [ ] Dice scraper
  - [ ] Remote boards (We Work Remotely, Remote OK)
  - [ ] Cross-board deduplication
  - [ ] systemd service files
  - [ ] Daily DB backup cron
- [ ] **Phase 6** — Analytics, outcome tracking, continuous improvement

## Dependencies

Core: `httpx[http2]`, `beautifulsoup4`, `lxml`, `aiosqlite`, `pydantic`, `pydantic-settings`, `typer`, `rich`, `pyyaml`, `apscheduler`, `fastapi`, `uvicorn`, `jinja2`, `playwright`, `anthropic`

See `pyproject.toml` for the full list.

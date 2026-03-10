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
│   │   ├── base.py            # Abstract base scraper (httpx + Playwright fallback)
│   │   └── indeed.py          # Indeed scraper (JSON-first parsing)
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

```yaml
target_roles:
  - "Lead Frontend Developer"
  - "Web Development Lead"
  - "Front End Engineering Manager"
  - "Senior Frontend Developer"
  - "Development Manager"

skills:
  must_have:       # LLM matches on ANY of these, not all
    - "JavaScript"
    - "React"
    - "TypeScript"
    - "HTML"
    - "CSS"
  nice_to_have:    # Bonus points
    - "Node.js"
    - "AEM"
    - "Angular"
    - "Python"
    - "Azure"
    - "CI/CD"
    - "RESTful APIs"
    - "SQL"
    - "Git"
    - "Redux"
    - "Bootstrap"

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
```

### boards.yaml (board-specific overrides only)

Boards inherit `location`, `search_queries`, and `radius_miles` from profile.yaml unless explicitly overridden.

```yaml
boards:
  indeed:
    enabled: true
    module: "app.scrapers.indeed"
    base_url: "https://www.indeed.com"
    # search_queries: inherited from profile.target_roles
    # location: inherited from profile.preferences.location
    # radius_miles: inherited from profile.preferences.max_commute_miles
    max_pages: 3
    delay_min: 10.0
    delay_max: 30.0
```

## CLI Commands

| Command | Description |
|---------|-------------|
| `job-agent init-db` | Create/migrate the database |
| `job-agent scrape --board indeed` | Scrape Indeed for new listings |
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
| `job-agent polish --id 5` | Generate a cover letter for job #5 |
| `job-agent polish --id 5 --resume ~/agent/resumes/resume.pdf` | Use a specific resume |
| `job-agent status` | Database stats by status |

## Evaluation Scoring

The local LLM scores each job from 0.0 to 1.0 against your profile:

| Score | Status | Meaning |
|-------|--------|---------|
| >= 0.7 | `evaluated` | Ready for review — strong match |
| 0.4 - 0.7 | `maybe` | Worth a look, partial match |
| < 0.4 | `rejected` | Auto-rejected (reason stored in DB) |

Scoring rules: matching ANY must-have skill is a positive signal (not all required). Missing salary info is treated as neutral. Only clearly irrelevant jobs score below 0.3.

## Cover Letter Generation (Phase 4)

When you find a job worth applying to, generate a tailored cover letter using the Anthropic Claude API.

### Setup

1. Get an API key at [console.anthropic.com](https://console.anthropic.com)
2. Add to `config/secrets.env`:

```env
ANTHROPIC_API_KEY=sk-ant-your-key-here
```

3. Put your resume in the `resumes/` directory:

```bash
mkdir -p ~/agent/resumes
# scp your resume from your PC
```

### Usage

From the CLI:

```bash
job-agent polish --id 5
```

Or from the dashboard: open any job detail page and click "Generate Cover Letter". The letter is saved to the database and shown on the detail page with a "Copy to clipboard" button.

The polisher automatically picks a template (leadership vs senior IC) based on the job title, sends your resume + the job description to Claude Sonnet, and gets back a tailored 3-4 paragraph letter. Cost is roughly $0.03 per letter.

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
- [x] **Phase 3** — FastAPI + HTMX web dashboard (Telegram deferred)
- [x] **Phase 4** — Cloud API cover letter polishing (Anthropic Claude)
- [ ] **Phase 5** — Additional scrapers (Dice, LinkedIn, remote boards) + hardening
- [ ] **Phase 6** — Analytics, outcome tracking, continuous improvement

## Dependencies

Core: `httpx[http2]`, `beautifulsoup4`, `lxml`, `aiosqlite`, `pydantic`, `pydantic-settings`, `typer`, `rich`, `pyyaml`, `apscheduler`, `fastapi`, `uvicorn`, `jinja2`, `playwright`

See `pyproject.toml` for the full list.

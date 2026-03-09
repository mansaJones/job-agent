# Job Search Agent — Jetson Orin Nano 8GB + 1TB SSD

## Hardware Profile

- **Board:** Jetson Orin Nano 8GB (shared CPU/GPU RAM)
- **Storage:** 1TB NVMe SSD
- **Available RAM for apps:** ~6GB after OS/JetPack overhead
- **Power:** 7-15W, designed for 24/7 operation
- **Access:** SSH over LAN (Tailscale recommended for remote)

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────┐
│                   JETSON ORIN NANO                       │
│                                                         │
│  ┌───────────┐    ┌──────────────┐    ┌──────────────┐  │
│  │  Scraper   │───▶│  SQLite DB   │◀───│  Ollama      │  │
│  │  Service   │    │  (1TB SSD)   │    │  (Local LLM) │  │
│  └───────────┘    └──────┬───────┘    └──────────────┘  │
│                          │                               │
│                   ┌──────┴───────┐                       │
│                   │  FastAPI     │                       │
│                   │  Dashboard   │                       │
│                   └──────┬───────┘                       │
│                          │                               │
└──────────────────────────┼──────────────────────────────┘
                           │
              ┌────────────┼────────────┐
              ▼            ▼            ▼
         Browser      Telegram     Cloud API
         (Review)      (Alerts)    (Polishing)
```

---

## Disk Layout (1TB SSD)

```
/home/agent/
├── data/
│   ├── jobs.db                  # SQLite database (WAL mode)
│   ├── jobs.db-wal              # WAL file
│   ├── backups/                 # Daily DB snapshots (cron)
│   └── html_snapshots/          # Raw HTML for parser debugging
├── models/                      # Ollama model weights (~4-5GB)
├── resumes/                     # Your resume(s), structured JSON + PDF
├── templates/                   # Cover letter templates
├── logs/                        # Rotating app logs
├── config/
│   ├── profile.yaml             # Your job preferences / target profile
│   ├── boards.yaml              # Per-board scraper config
│   └── secrets.env              # API keys, Telegram bot token
└── app/
    ├── scrapers/                # Per-board scraper modules
    ├── evaluator/               # Local LLM scoring logic
    ├── polisher/                # Cloud API cover letter generation
    ├── dashboard/               # FastAPI + frontend
    ├── notifier/                # Telegram bot
    └── scheduler/               # Orchestration / cron logic
```

With 1TB you can comfortably store years of job data, full HTML snapshots for every
listing (useful for debugging broken parsers), model weights, and daily DB backups
without ever thinking about disk space. Budget ~5GB for model weights, ~1GB for a
year of job data + snapshots, and the rest is yours.

---

## Phase 1: Foundation (Week 1-2)

**Goal:** Board running, Ollama serving, database ready, one scraper working.

### 1.1 System Setup
- Verify JetPack version and CUDA availability
- Install Ollama (ARM64 build with CUDA support)
- Pull initial model: `ollama pull llama3.1:8b-instruct-q4_K_M`
  - Alternative: `phi3:mini` if you want more RAM headroom
- Install Python 3.10+, pip, virtualenv
- Install Node.js (for dashboard frontend if needed)

### 1.2 Database
- SQLite3 with WAL mode enabled (better concurrent read/write)
- Core schema:

```sql
CREATE TABLE jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,           -- 'indeed', 'dice', 'linkedin', etc.
    external_id TEXT,               -- source-specific job ID
    url TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    company TEXT,
    location TEXT,
    salary_min REAL,
    salary_max REAL,
    description TEXT,
    raw_html TEXT,                  -- full HTML snapshot
    date_posted TEXT,
    date_scraped TEXT DEFAULT (datetime('now')),
    status TEXT DEFAULT 'new'       -- new, evaluated, approved, rejected, applied
);

CREATE TABLE evaluations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    model_used TEXT NOT NULL,       -- 'llama3.1:8b-q4' or 'claude-sonnet'
    match_score REAL,              -- 0.0 to 1.0
    reasoning TEXT,                 -- LLM's explanation
    cover_letter_draft TEXT,
    evaluated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    decision TEXT NOT NULL,         -- 'approved', 'rejected', 'maybe'
    notes TEXT,                     -- your notes
    decided_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE applied (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    method TEXT,                    -- 'manual', 'assisted'
    applied_at TEXT DEFAULT (datetime('now')),
    follow_up_date TEXT
);

-- Indexes
CREATE UNIQUE INDEX idx_jobs_source_ext ON jobs(source, external_id);
CREATE INDEX idx_jobs_status ON jobs(status);
CREATE INDEX idx_evaluations_score ON evaluations(match_score);
```

### 1.3 Job Profile Config

```yaml
# config/profile.yaml
target_roles:
  - "Systems Administrator"
  - "DevOps Engineer"
  - "Cloud Engineer"
  - "Network Engineer"
  # add your actual targets

skills:
  must_have:
    - "Linux"
    - "AWS"
  nice_to_have:
    - "Kubernetes"
    - "Terraform"
    - "Ansible"

preferences:
  location: "Country Club Hills, IL"
  remote_ok: true
  hybrid_ok: true
  onsite_ok: false
  max_commute_miles: 40
  salary_min: 80000
  experience_years: 5            # your actual experience

blacklist:
  companies:
    - "Company You Hate Inc."
  keywords:
    - "unpaid"
    - "intern"
    - "clearance required"        # unless you have one
```

### 1.4 First Scraper (Indeed)
- Use `httpx` + `BeautifulSoup4` (lighter than Selenium for initial scraping)
- Fall back to Playwright for JS-rendered pages
- Respect rate limits: random delays 10-30s between requests
- Store raw HTML in `html_snapshots/` directory for parser debugging
- Deduplicate by URL before inserting into DB

---

## Phase 2: Local Evaluation (Week 2-3)

**Goal:** LLM scores every new job against your profile.

### 2.1 Evaluation Pipeline
- Query Ollama HTTP API (`localhost:11434/api/generate`)
- Prompt structure:

```
You are a job matching assistant. Given the candidate profile and job posting below,
score the match from 0.0 to 1.0 and explain your reasoning in 2-3 sentences.

CANDIDATE PROFILE:
{contents of profile.yaml}

JOB POSTING:
Title: {title}
Company: {company}
Location: {location}
Description: {description}

Respond in JSON:
{"score": 0.0-1.0, "reasoning": "...", "red_flags": [...], "highlights": [...]}
```

- Run evaluations in batch during off-hours (e.g., 2am-6am)
- At ~10-15 tokens/sec, expect ~30-60 seconds per evaluation
- 100 jobs/night is very achievable

### 2.2 Auto-filtering
- Score >= 0.7: mark as "ready for review"
- Score 0.4-0.7: mark as "maybe"
- Score < 0.4: auto-reject (still stored, just hidden from dashboard by default)
- Any blacklisted company/keyword: auto-reject regardless of score

---

## Phase 3: Dashboard & Notifications (Week 3-4)

**Goal:** You can review matches from your phone/laptop.

### 3.1 FastAPI Dashboard
- Endpoints:
  - `GET /jobs` — list jobs by status, sortable by score
  - `GET /jobs/{id}` — full job detail + evaluation + draft
  - `POST /jobs/{id}/decide` — approve/reject/maybe
  - `POST /jobs/{id}/polish` — trigger cloud API cover letter generation
  - `GET /stats` — scraping/evaluation/application stats
- Simple HTML frontend (HTMX or basic React)
- Run on port 8080, accessible on LAN

### 3.2 Telegram Bot
- Create bot via @BotFather
- Notifications:
  - Daily digest: "14 new matches, 6 scored above 0.7"
  - Inline buttons: [View Dashboard] [Pause Scraping]
- Quick actions from Telegram if you want (approve/reject by reply)

### 3.3 Remote Access
- Install Tailscale on the Orin for secure remote access without port forwarding
- Access dashboard from anywhere: `http://orin-nano:8080`

---

## Phase 4: Cloud Polishing (Week 4-5)

**Goal:** Approved jobs get polished, tailored cover letters.

### 4.1 Cloud API Integration
- When you click "Approve" on the dashboard, trigger a cloud API call
- Send: job description + your resume + local LLM's draft as starting point
- Receive: polished, tailored cover letter
- Recommended: Anthropic Claude API (Sonnet for cost efficiency)
- Store the polished version in `evaluations` table
- Expected cost: < $0.05 per cover letter, probably $3-5/month total

### 4.2 Cover Letter Templates
- Keep 2-3 base templates in `templates/` for different role types
- Cloud API adapts the appropriate template to each specific job
- You review + edit the final version before using it

---

## Phase 5: More Scrapers + Hardening (Week 5-7)

**Goal:** Full board coverage, resilience, reliability.

### 5.1 Additional Scrapers
- Dice (relatively scraper-friendly, tech-focused)
- Glassdoor (requires auth, use cautiously)
- LinkedIn (via Google dorking: `site:linkedin.com/jobs "keyword"`)
- Remote boards: We Work Remotely, Remote OK (clean HTML, easy to parse)

### 5.2 Parser Health Monitoring
- Each scraper run logs: jobs found, jobs parsed, errors
- If a scraper returns 0 results or >50% parse errors → auto-disable + alert
- Store the raw HTML so you can fix the parser without re-scraping
- LLM fallback parser: feed raw HTML to local model for extraction
  when structured parsing breaks

### 5.3 Resilience
- systemd services for: Ollama, scraper scheduler, dashboard
- Auto-restart on failure with backoff
- SQLite WAL mode + daily backup cron job
- Log rotation (don't fill even 1TB with logs, that would be impressive but dumb)

```ini
# /etc/systemd/system/job-agent-dashboard.service
[Unit]
Description=Job Agent Dashboard
After=network.target ollama.service

[Service]
Type=simple
User=agent
WorkingDirectory=/home/agent/app
ExecStart=/home/agent/venv/bin/python -m uvicorn dashboard.main:app --host 0.0.0.0 --port 8080
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
# Daily backup cron (add to crontab)
0 3 * * * cp /home/agent/data/jobs.db /home/agent/data/backups/jobs_$(date +\%Y\%m\%d).db
# Cleanup backups older than 30 days
0 4 * * * find /home/agent/data/backups -name "*.db" -mtime +30 -delete
```

---

## Phase 6: Polish & Iterate (Ongoing)

- Track application outcomes (interview? rejection? ghosted?)
- Use outcome data to fine-tune your scoring prompt
- Add resume tailoring (adjust resume bullet emphasis per job)
- Add deduplication across boards (same job posted on Indeed AND Dice)
- Dashboard analytics: applications/week, response rate, score distribution

---

## Tech Stack Summary

| Component        | Tool                          | Why                              |
|-----------------|-------------------------------|----------------------------------|
| OS              | JetPack (Ubuntu-based)        | Already flashed                  |
| Local LLM       | Ollama + Llama 3.1 8B Q4     | Free, CUDA-accelerated, good enough |
| Cloud LLM       | Anthropic Claude API (Sonnet) | Quality cover letters, cheap     |
| Database        | SQLite (WAL mode)             | Zero overhead, perfect for single-user |
| Scraping        | httpx + BeautifulSoup4        | Lightweight, async-capable       |
| Browser auto    | Playwright (fallback)         | JS-rendered pages only           |
| Backend API     | FastAPI + Uvicorn             | Fast, async, easy                |
| Frontend        | HTMX or lightweight React     | Keep it simple                   |
| Notifications   | python-telegram-bot           | Free, instant push               |
| Remote access   | Tailscale                     | Zero-config VPN, secure          |
| Process mgmt    | systemd                       | Auto-start, auto-restart         |
| Scheduling      | APScheduler or cron           | Job orchestration                |
| Backups         | cron + cp                     | It's SQLite, keep it simple      |

---

## Estimated Timeline

| Phase | What                        | Duration   |
|-------|-----------------------------|------------|
| 1     | Foundation + first scraper  | 1-2 weeks  |
| 2     | Local LLM evaluation        | 1 week     |
| 3     | Dashboard + Telegram        | 1-2 weeks  |
| 4     | Cloud API polishing         | 1 week     |
| 5     | More scrapers + hardening   | 2 weeks    |
| 6     | Iterate forever             | Ongoing    |

**Total to MVP (Phases 1-3): ~4 weeks**
**Total to full system: ~7 weeks**

---

## Estimated Monthly Costs

| Item                  | Cost       |
|-----------------------|------------|
| Electricity (24/7)    | ~$1-2      |
| Cloud API (polishing) | ~$3-5      |
| Telegram              | Free       |
| Tailscale (personal)  | Free       |
| **Total**             | **~$5-7**  |

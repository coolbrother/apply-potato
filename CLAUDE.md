# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install dependencies (after setup_wizard.py creates venv)
pip install -r requirements.txt

# Run tests
pytest

# Run a single test file
pytest tests/test_filters.py

# Run a single test by name
pytest tests/test_filters.py::test_class_standing_filter -v

# Job scraping
python scrape_jobs.py              # Run once
python scrape_jobs.py --scheduled  # Daemon mode (every N minutes)
python scrape_jobs.py --limit 5    # Cap at 5 new jobs this run
python scrape_jobs.py --url <URL>  # Single-URL debug run
python scrape_jobs.py --clear-filtered  # Reset filtered_jobs.json cache

# Gmail status monitoring
python check_gmail.py              # Run once
python check_gmail.py --scheduled  # Daemon mode
python check_gmail.py --reprocess  # Re-classify already-seen emails

# First-time setup
python setup_wizard.py             # Creates venv, installs deps, configures OAuth
python install_service.py          # Installs as Windows/macOS background service
```

## Architecture

Two independent entry points (`scrape_jobs.py`, `check_gmail.py`) share the `src/` module library. They can run concurrently.

### Job scraping pipeline (`scrape_jobs.py`)

1. **Discovery** — `GitHubParser` fetches raw markdown from GitHub repos and parses job tables; `NewsletterParser` reads newsletter HTML emails from Gmail. Both return `JobListing[]`.
2. **Dedup** — `Deduplicator` checks three caches: Sheets URLs already added, `filtered_jobs.json` (previously rejected), `seen_sources.json` (source URLs with TTL). Skip if already seen.
3. **Scrape** — `PlaywrightScraper` (persistent headed Chrome, anti-detection) fetches the job page and returns `(content, final_url, is_blocked)`.
4. **Extract** — `AIExtractor` calls OpenAI or Gemini with `prompts/job_extraction.txt` and returns one or more `ExtractedJob` dataclass instances (20+ fields).
5. **Filter** — `passes_hard_filters()` in `filters.py` applies binary eligibility checks (class standing, graduation timeline, work auth, job type, season/year). Failures go into `filtered_jobs.json`.
6. **Score** — `calculate_fit_score()` in `scoring.py` returns 0–100 with notes (informational only, does not gate).
7. **Sync** — `SheetsClient.add_job()` writes a row to Google Sheets.
8. **Notify** — If a dream company job, Discord webhook fires. Optionally, `AutoApplyOrchestrator` detects form fields and generates tailored docs.

### Gmail status pipeline (`check_gmail.py`)

Fetch emails from Primary inbox → privacy filters (`email_filters.py`) → AI classification (`email_classifier.py`, same provider config) → fuzzy-match company to a Sheets row → update status + color + notes → Discord notify on dream company changes.

### Configuration (`src/config.py`)

Single `.env` file (see `.env.example`). `get_config()` is a module-level singleton returning a `Config` dataclass. Everything lives in config: API keys, user profile (class standing, graduation, major, skills, work auth), filter preferences, AI provider, Discord, auto-apply settings.

### Key modules

| Module | Responsibility |
|---|---|
| `src/github_parser.py` | Parse GitHub markdown/HTML job tables |
| `src/newsletter_parser.py` | Parse newsletter HTML from Gmail |
| `src/scraper.py` | Playwright browser, anti-detection, special site handling (Simplify, Greenhouse) |
| `src/ai_extractor.py` | OpenAI/Gemini job extraction (20+ fields), retry logic |
| `src/filters.py` | Hard eligibility filters (binary) |
| `src/scoring.py` | Soft fit score 0–100 |
| `src/deduplication.py` | URL normalization + three-tier caching |
| `src/sheets.py` | Google Sheets CRUD, 18-column schema, color formatting |
| `src/gmail.py` | Gmail API client, OAuth, Primary inbox only |
| `src/email_classifier.py` | AI email classification → status category |
| `src/email_filters.py` | Pre-AI noise filter for automated/transactional mail |
| `src/notifications.py` | Discord webhook alerts |
| `src/auto_apply.py` | Dream company form detection + doc generation |
| `src/docx_utils.py` | Word document manipulation for resume/cover letter |
| `prompts/` | Plain-text AI prompt templates loaded at runtime |

### State & persistence

- **Google Sheets** — primary job database; status, notes, dates, fit score
- `data/filtered_jobs.json` — URLs that failed hard filters (skipped on re-runs)
- `data/seen_sources.json` — GitHub/newsletter source URLs with TTL
- `data/processed_emails.json` — Gmail message IDs already handled
- `data/extraction_failures.json` — URLs where AI extraction failed
- `auth/` — OAuth tokens (`gmail_token.json`, `sheets_token.json`); auto-refreshed, git-ignored
- `browser-profile/` — Playwright persistent profile for anti-detection continuity

### AI provider abstraction

`OPENAI_API_KEY` vs `GEMINI_API_KEY` selects the provider; the same prompt files and `ExtractedJob` schema are used for both. `AUTO_APPLY_PROVIDER` can separately be `claude`, `openai`, or `gemini` for document generation.

### Testing

`tests/conftest.py` provides mocked `Config`, `SheetsClient`, and `EmailMessage` fixtures. Tests are unit-level with mocked external calls. `scripts/test_e2e.py` does live end-to-end runs; `scripts/test_form_fill.py` tests Chrome DevTools auto-apply integration.

### Other Directions

Always test new features before reporting that they are done.
Never commit .env or other sensitive files.

When creating a plan, update TASKS.md with the goals and the steps to finish that plan before executing that plan.

Upon finishing a task, update TASKS.md and SESSION.md with what happened.

Never import in the middle of the file.
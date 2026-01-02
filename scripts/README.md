# Scripts

Manual test scripts for debugging and testing pipeline components. These scripts require external resources (network, API keys, Google credentials).

For automated unit tests with self-contained fixtures, see `tests/`.

## Scripts

### test_e2e.py

Unified pipeline test script. Supports testing individual components or the full pipeline.

**Input Sources:**
- Single URL: `python test_e2e.py <url>`
- GitHub job lists: `python test_e2e.py --from-github`

**Pipeline Stages:**
```
GitHub Parser → Scraper → AI Extraction → Filters → Scoring → Sheets
     ↑             ↑            ↑            ↑          ↑        ↑
--parse-only  --scrape-only  --extract-only  |←── full pipeline ──→|
```

**Usage:**

```bash
# Full pipeline from URL (dry run)
python test_e2e.py https://jobs.lever.co/company/job-id

# Full pipeline from URL + add to Google Sheets
python test_e2e.py https://jobs.lever.co/company/job-id --save

# Full pipeline from GitHub (1 job)
python test_e2e.py --from-github

# Full pipeline from GitHub (5 jobs)
python test_e2e.py --from-github --count 5

# GitHub parsing only (list jobs, no scraping)
python test_e2e.py --from-github --parse-only

# Scraping only (no AI extraction)
python test_e2e.py https://example.com/job --scrape-only

# AI extraction only (no filtering/scoring)
python test_e2e.py https://example.com/job --extract-only

# Save scraped content for offline AI testing
python test_e2e.py https://example.com/job --save-content ./scraped_content/
python test_e2e.py --from-github --count 10 --scrape-only --save-content ./scraped_content/
```

**Options:**

| Option | Description |
|--------|-------------|
| `<url>` | Job posting URL to test |
| `--from-github` | Fetch jobs from configured GitHub repos |
| `--count N` | Number of jobs to process from GitHub (default: 1) |
| `--parse-only` | Stop after GitHub parsing (requires `--from-github`) |
| `--scrape-only` | Stop after scraping, show content preview |
| `--extract-only` | Stop after AI extraction, skip filters/scoring |
| `--save` | Add qualifying jobs to Google Sheets |
| `--save-content <path>` | Save scraped content to file or directory |

---

### test_ai_extractor.py

Offline AI extraction testing. Uses saved content files (no network scraping needed). Useful for iterating on AI prompts without waiting for page loads.

**Prerequisites:** Run `test_e2e.py --save-content ./scraped_content/` first to generate test files.

**Usage:**

```bash
# Test all files in scraped_content/
python test_ai_extractor.py

# Test specific file
python test_ai_extractor.py --file ./scraped_content/example.txt

# Limit to first N files
python test_ai_extractor.py --count 3

# Save extracted JSON for inspection
python test_ai_extractor.py --save
```

**Options:**

| Option | Description |
|--------|-------------|
| `--file <path>` | Test a specific content file |
| `--count N` | Only process first N files (default: all) |
| `--save` | Save extracted jobs as JSON to `extracted_jobs/` |

---

## Typical Workflows

### Debug GitHub parser issues
```bash
python test_e2e.py --from-github --parse-only
```

### Debug scraper issues (page not loading, wrong content)
```bash
python test_e2e.py https://example.com/job --scrape-only
```

### Debug AI extraction issues
```bash
# Save content first
python test_e2e.py https://example.com/job --save-content ./scraped_content/job.txt

# Iterate on AI prompts (edit prompts/job_extraction.txt, re-run)
python test_ai_extractor.py --file ./scraped_content/job.txt
```

### Debug filter/scoring issues
```bash
python test_e2e.py https://example.com/job
# Check logs for filter results and score breakdown
```

### Build test fixture library
```bash
# Scrape multiple jobs and save for offline testing
python test_e2e.py --from-github --count 20 --scrape-only --save-content ./scraped_content/
```

---

## Output

- Logs: `logs/e2e.log`
- Scraped content: `scraped_content/` (when using `--save-content`)
- Extracted JSON: `extracted_jobs/` (when using `--save` with test_ai_extractor.py)

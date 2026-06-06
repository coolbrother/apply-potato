---
name: discover-job
description: >
  Runs Phase 1 of the apply-potato pipeline on a single job URL: scrapes the page,
  extracts structured data, detects whether a resume/cover letter is needed (for dream
  companies), saves the raw + formatted job description to the Resume folder, and writes
  a row to Google Sheets with the Resume/Cover Letter columns populated. Use this skill
  whenever the user says anything like "test this url", "run the pipeline on this job",
  "discover job at [url]", "check this posting", "scrape this job URL", or pastes a job
  URL and asks you to process or record it. Also trigger when the user wants to verify
  that Phase 1 is working correctly for a given posting.
---

# discover-job

This skill runs **Phase 1** of the apply-potato job scraping pipeline on a single URL
and reports the results — without needing to type a terminal command.

## What it does

1. Extracts the job URL from the user's message
2. Runs `python scrape_jobs.py --url <URL>` from the project root
3. Reads the output log to summarize: what was extracted, whether it passed filters,
   the Sheets row number, the Resume/Cover Letter detection results, and the path to
   the saved job description file
4. Reports any errors clearly (blocked page, filter failure, Claude CLI missing, etc.)

## Steps

1. **Find the URL** — look for a `http://` or `https://` URL in the user's message.
   If none is found, ask: "What job URL should I run the pipeline on?"

2. **Run the pipeline**:
   ```
   cd <project-root>
   python scrape_jobs.py --url <URL>
   ```
   Capture stdout+stderr. This may take 30–90 seconds (browser + AI extraction).

3. **Parse and report results** — show the user:
   - ✅/❌ Whether the job passed hard filters (and the reason if filtered out)
   - Row number in Google Sheets (if added)
   - Resume needed: Yes / No / (detection skipped — not a dream company)
   - Cover Letter needed: Yes / No / (detection skipped)
   - Path to saved job description file (e.g. `$JOB_DESC_OUTPUT_DIR/42_Google/`)
   - Whether the folder was committed + pushed to the Resume repo
   - Any warnings or errors that appeared

4. **Optionally run tests** — if the user asks to also verify the test suite, run:
   ```
   pytest tests/test_job_desc.py -v
   ```
   and show a pass/fail summary.

## Error handling

- If scrape_jobs.py exits non-zero, show the last 20 lines of stderr
- If the job was a duplicate/already in Sheets, report that clearly
- If Claude CLI wasn't found for job desc formatting, note the fallback was used
- If the Resume repo push failed (e.g. no remote set up), note it but don't treat it as a failure

## Example output to user

```
✅ Job processed successfully

Company: Google
Title: Software Engineering Intern
Row: 47
Fit Score: 82

Resume needed: Yes (dream company — form detected resume upload field)
Cover Letter: No

Job description saved:
  $JOB_DESC_OUTPUT_DIR/47_Google/47_Google_job_desc.md
  Pushed to Resume repo ✓
```

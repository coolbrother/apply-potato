---
name: apply
description: >
  Runs the complete apply-potato pipeline for a single job URL: Phase 1 (discover + scrape +
  extract + filter + score + add to Sheets), Phase 2 (generate tailored resume and cover
  letter), and Phase 3 (open the application form in the browser, fill every field, upload
  docs, leave the tab open for review). Use this skill whenever the user says things like
  "apply to this job", "run the full pipeline on [url]", "process this job end to end",
  "do everything for this posting", or pastes a URL and asks you to handle the whole thing.
  Never submits — always leaves the filled form open for the user to review and submit.
---

# apply

Run all three phases of the apply-potato pipeline for one job URL. Each phase is reported
as it completes. The form is never submitted — the browser tab stays open for manual review.

## Step 1: Get the URL

Look for a `http://` or `https://` URL in the user's message. If none is found, ask:
"What job URL should I run the full pipeline on?"

## Step 2: Phase 1 — Discover

Run from the project root:
```
python scrape_jobs.py --url <URL>
```
Capture stdout+stderr. This may take 30–90 seconds (Playwright + AI extraction).

Parse the output for:
- **Row number** — look for `Row:` or `Added to Sheets` patterns
- **Filter result** — did the job pass hard filters? If not, what was the reason?
- **Resume needed** / **Cover Letter needed** — Yes or No
- **Fit score** — if present
- **Dream company** — Yes or No

**If the job was filtered out:** report the reason and stop. Do not run Phases 2 or 3.

**If the job was a duplicate** (already in Sheets): report the existing row number and ask:
"This job is already in Sheets at row N. Run Phase 2 and 3 anyway?"
If the user says yes, use that row number and continue to Phase 2.

Report Phase 1 result before continuing:
```
Phase 1 ✅
Company: [company]
Title:   [title]
Row:     [N]
Score:   [score]
Resume:  [Yes/No]  Cover Letter: [Yes/No]
Dream:   [Yes/No]
```

## Step 3: Phase 2 — Generate docs

Run from the project root:
```
python generate_docs.py <row>
```
Capture stdout+stderr. This may take 2–5 minutes (resume tailoring + cover letter).

Parse the output for which documents were generated and their paths.

**If Phase 2 fails:** report the error, then ask: "Phase 2 failed — continue to Phase 3 anyway
(form fill without tailored docs)?" If yes, proceed with whatever docs exist in the folder.

Report Phase 2 result before continuing:
```
Phase 2 ✅
  [path/to/Resume.docx]
  [path/to/Cover_Letter.docx]   (or: Cover letter not needed — skipped)
Pushed to Resume repo ✓/✗
```

## Step 4: Phase 3 — Fill form

Invoke the `/fill-form` skill with the row number from Phase 1:
```
/fill-form <row>
```

This opens the application in the browser, fills all fields from `applicant_info.txt`,
uploads the docs from the job folder, and stops before Submit.

Report Phase 3 result:
```
Phase 3 ✅
Form filled — tab left open for review.
[Any fields that could not be filled]
```

## Final summary

After all three phases complete, show a compact summary:
```
Pipeline complete for [Company] — [Title]

Phase 1 ✅  Row [N] added to Sheets
Phase 2 ✅  Resume + Cover Letter generated
Phase 3 ✅  Form filled — tab open for review

Next step: review the form in the browser and click Submit when ready.
```

## Error handling

- Phase 1 filtered → stop, report reason
- Phase 1 blocked (scrape failed) → stop, suggest trying `--url` manually or checking the site
- Phase 2 fails → ask before proceeding to Phase 3
- Phase 3 form not found / already submitted → report and leave to user
- Any phase timeout → report and stop

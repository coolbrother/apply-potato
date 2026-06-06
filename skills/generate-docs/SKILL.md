---
name: generate-docs
description: >
  Runs Phase 2 of the apply-potato pipeline: generates the tailored resume and/or cover
  letter for a job that was already recorded in Google Sheets during Phase 1. Use this
  skill whenever the user says anything like "generate the docs", "generate the resume and
  cover letter", "run phase 2", "make the documents for row 3013", "tailor docs for
  3013_Pathos", or otherwise wants to produce the application documents for an
  already-discovered job. Accepts a row number (e.g. 3013), a folder stem (e.g.
  3013_Pathos), or "all" to process every recent row that still needs docs.
---

# generate-docs

This skill runs **Phase 2** of the apply-potato pipeline — generating the tailored
resume and/or cover letter for a job already recorded in Google Sheets — without
needing to type a terminal command.

## What it does

1. Figures out the target (row number, folder stem, or all recent rows)
2. Runs `python generate_docs.py <target>` from the project root
3. Reads the output log to summarize: which documents were generated, where they were
   saved, and whether the folder was pushed to the Resume repo
4. Reports any errors clearly (folder missing, Claude CLI missing, generation failed, etc.)

## Steps

1. **Find the target** — look in the user's message for:
   - A row number (e.g. `3013`)
   - A folder stem (e.g. `3013_Pathos`)
   - The word "all" → use `--all`

   If none is found, ask: "Which job should I generate documents for? Give me a row
   number (e.g. 3013), a folder stem (e.g. 3013_Pathos), or say 'all'."

2. **Run Phase 2**:
   ```
   cd <project-root>
   python generate_docs.py <target>          # or: python generate_docs.py --all
   ```
   Capture stdout+stderr. This may take 2–5 minutes per job (Claude tailoring the
   resume + writing the cover letter).

3. **Parse and report results** — show the user:
   - ✅ Which documents were generated (Resume / Cover Letter / both)
   - The folder + filenames (e.g. `3013_Pathos_Resume.docx`, `3013_Pathos_Cover_Letter.docx`)
   - Whether the folder was committed + pushed to the Resume repo
   - If docs already existed and were skipped (idempotent), say so
   - Any warnings or errors that appeared

## Error handling

- If generate_docs.py exits non-zero, show the last 20 lines of stderr
- If no Sheets row matched the target, report that clearly
- If the job's folder wasn't found (Phase 1 not run yet), note that Phase 1 must run first
- If the row needs no docs (Resume/Cover Letter both "No"), report nothing was generated
- If Claude CLI wasn't found for resume tailoring, note the fallback was used
- If the Resume repo push failed (e.g. no remote set up), note it but don't treat it as a failure

## Example output to user

```
✅ Documents generated

Row 3013 — Pathos (ML Engineer Intern)

Generated:
  $JOB_DESC_OUTPUT_DIR/3013_Pathos/3013_Pathos_Resume.docx
  $JOB_DESC_OUTPUT_DIR/3013_Pathos/3013_Pathos_Cover_Letter.docx

Pushed to Resume repo ✓
```

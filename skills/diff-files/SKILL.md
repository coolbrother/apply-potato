---
name: diff-files
description: >
  Compares two files and writes a diff.md containing ONLY the lines that differ
  (removed/added) between them. Handles .docx, .md, .txt, .xml, and other text formats.
  Use this skill whenever the user says anything like "diff these two files", "compare
  these files", "find the differences between X and Y", "write a diff of", or wants a
  diff.md produced for two given file paths. Also used by the apply-potato Phase 2
  pipeline to record what the resume tailoring changed (base resume vs tailored resume).
---

# diff-files

Writes a `diff.md` that contains **only the lines that differ** between two files —
removed lines (`-`) and added lines (`+`), with no unchanged context.

## Inputs

- **file1** — first (e.g. original/base) file path
- **file2** — second (e.g. modified/tailored) file path
- **output_path** *(optional)* — where to write the diff; defaults to
  `<directory of file2>/diff.md`

Supported formats: `.docx` (text extracted in-memory — no unpacking), `.md`, `.txt`,
`.xml`, and any other text-based file (read as plain text).

## Steps

1. **Resolve paths** — get file1, file2, and the output path from the user's message.
   If only two paths are given, the output defaults to `<dir of file2>/diff.md`.
   If fewer than two paths are given, ask which two files to compare.

2. **Run the bundled helper** (this does the actual, deterministic diff via difflib):
   ```
   python "<skill base dir>/scripts/linediff.py" "<file1>" "<file2>" "<output_path>"
   ```
   (Omit the third argument to use the default output location.)

3. **Report** — confirm the path the diff was written to, and a one-line note of how many
   lines changed (count the `+`/`-` lines in the result).

## Hard rules

- **Always** produce the diff by running `scripts/linediff.py`. Do NOT hand-write or
  eyeball the diff — the helper is deterministic and handles `.docx` extraction correctly.
- Do NOT open `.docx` files directly; the helper reads them in-memory via zip. There are
  no temp files to clean up.
- In automated/pipeline mode, stay within the resume output directory configured in `.env` (`JOB_DESC_OUTPUT_DIR`).

## allowedTools

`Bash,Read,Write`

## Example output to user

```
✅ Diff written

  $JOB_DESC_OUTPUT_DIR/3013_Pathos/diff.md

12 lines changed (5 removed, 7 added).
```

---
name: fill-form
description: >
  Fills out job application forms in the browser using Chrome DevTools MCP.
  Reads applicant data exclusively from applicant_info.txt, opens a new browser
  tab for each job, fills every answerable field, uploads the tailored resume
  and cover letter from the job's folder in the Resume repo, and stops before
  the Submit button — leaving each tab open for the user to review and submit.
  Use this skill whenever the user says things like "fill out the form",
  "fill this application", "auto-fill job applications", "fill forms for rows X Y Z",
  or "apply to these jobs". Requires Google Sheets row numbers as input (e.g.
  "fill forms for rows 3 and 7") so the skill can find both the apply URL and
  the matching tailored resume/cover letter in the Resume folder.
  Also supports "auto" mode: "fill forms auto" scans the last 10 rows with status
  "New" and processes them automatically, then sends a Discord notification
  summarising the result of each form (ready for review, no longer exists, etc.).
---

# fill-form

Fills job application forms in the browser via Chrome DevTools MCP, using only
`applicant_info.txt` as the personal-data source and uploading the tailored
resume/cover letter from the job's folder. Never submits — leaves each tab open
for the user to review and submit manually.

> **Interruption:** If the user interrupts at any point (Ctrl+C, Escape, or
> any natural break), every open browser tab stays open under full manual
> control. No form will ever be auto-submitted.

## Why row numbers are required

Each job's tailored resume and cover letter live in a folder named
`{row}_{Company}/` inside the Resume repo (e.g. `47_Google/`). A raw URL alone
cannot locate that folder. Row numbers give us everything: the apply URL (from
Google Sheets column B), the folder path, and the generated docs inside it.

If the user provides a URL instead of a row number, ask:
> "Which sheet row does that job correspond to? I need the row number to find
> the tailored resume and cover letter for this application."

## Data source rule

`applicant_info.txt` is the **ONLY** source of personal information for form
fields (name, email, phone, etc.).
Read it once with the `Read` tool before filling any form.
Do **NOT** use training data, browser autofill, history, or any other file.
If a field cannot be answered from `applicant_info.txt`, skip it.

The file lives at:
```
<project-root>/applicant_info.txt
```

---

## AUTO MODE

Triggered when the user says **"auto"** (e.g. "fill forms auto", "run auto",
"fill-form auto").

### Auto Step 0 — Determine Discord webhook

Check if the user's message contains a Discord webhook URL
(`https://discord.com/api/webhooks/...`). If so, use it.
Otherwise use the webhook configured in `.env` (`DISCORD_WEBHOOK_URL`).
If neither is available, note that the notification will be skipped and continue.

Store the webhook for use in Auto Step 3.

### Auto Step 1 — Fetch last 10 "New" rows

```
cd <project-root>
python scripts/get_apply_urls.py --auto 10
```

Parse the JSON. These are the last (most recently added) rows in Google Sheets
whose status is "New" or empty. If the list is empty, report:
> "No rows with status 'New' found — nothing to fill."
and stop.

### Auto Step 2 — Read applicant info (once)

```
Read: <project-root>/applicant_info.txt
```

### Auto Step 3 — Process each row

Maintain a **results list** — one entry per row, updated as you go:

```
{ row, company, position, status, notes }
```

Possible `status` values:
- `"ready_for_review"` — form filled, stopped before submit, tab open
- `"no_longer_exists"` — page 404'd, redirected to job board, or was blank/error
- `"login_required"` — page redirected to a sign-in wall (e.g. Amazon, LinkedIn, Workday SSO); credentials not in applicant_info.txt
- `"no_url"` — row had no apply URL in the sheet
- `"fill_error"` — navigation succeeded but form filling failed unexpectedly

For each row (in order):

**a. Check URL** — if empty, set status `"no_url"` and continue to the next row.

**b. Open tab + navigate**
```
mcp__chrome-devtools__new_page
mcp__chrome-devtools__navigate_page  →  <apply_url>
```
Wait 2 seconds, then take a snapshot. Detect whether the page is valid:
- Valid: contains form fields, Apply button, or job title text
- Invalid (no longer exists): 404 page, "job no longer available", redirect to job
  board homepage, or completely blank content

**Login wall detection:** After navigation (including any redirect), check if the current URL or page content indicates a sign-in page (e.g. URL contains "login", "signin", "passport", "auth"; page heading says "Log in", "Sign in"). If so: set status `"login_required"`, report explicitly to the user:
> "Row N — Company: requires account login (redirected to [URL]). Credentials not in applicant_info.txt. Tab left open for manual login."
Leave the tab open and continue to the next row. Do NOT close it silently.

If **invalid** (404 / job gone): set status `"no_longer_exists"`, close the tab
(`mcp__chrome-devtools__close_page`), and:
  - If more rows remain → continue to the next row
  - If this is the **last row** → do NOT close the tab; instead pause and report
    to the user: "The last job (Row N — Company) no longer exists. Tab is open
    for you to verify. Continue when ready." Wait for the user to say "continue"
    or "done", then close the tab and proceed to Auto Step 4.

**c. Fill the form** — follow Steps 3c–3e from normal mode exactly.
   Set status `"ready_for_review"` when the form is filled and stopped before submit.
   Set status `"fill_error"` if an unrecoverable error occurs mid-fill.

   After setting status `"ready_for_review"`, record the fill (non-fatal):
   ```
   python scripts/update_filled_forms.py <row> "<company>" "<position>"
   ```

### Auto Step 4 — Discord notification

Build a summary message and send it:

```
cd <project-root>
python scripts/send_discord.py --webhook <url> --message "<summary>"
```

**Message format:**
```
🥔 apply-potato — Form Fill Summary (auto, N jobs)

✅ Row 42 — Acme Corp / SWE Intern → Ready for review
❌ Row 7  — Initech / Data Analyst → Job no longer exists
⚠️ Row 15 — Stripe / PM Intern    → No apply URL in sheet
💥 Row 9  — Figma / Design Intern → Fill error

Tabs still open: 1  |  Run: <timestamp>
```

Icons:
- ✅ `ready_for_review`
- ❌ `no_longer_exists`
- 🔒 `login_required`
- ⚠️ `no_url`
- 💥 `fill_error`

If the webhook is unavailable, print the summary in the conversation instead.

### Auto Step 5 — Report to user

After the notification is sent, summarise in the conversation:

```
Auto run complete — N jobs processed.

Ready for review (tabs open):
  Row 42 — Acme Corp / SWE Intern

Skipped / issues:
  Row 7  — Initech → job no longer exists
  Row 15 — Stripe  → no apply URL

Discord notification sent ✓
```

Do NOT wait for "done" in auto mode — the user will close tabs manually after
reviewing and submitting. The skill is finished.

---

## NORMAL MODE (row numbers)

### Step 0 — Parse inputs

Extract row numbers from the user's message (digits, optionally preceded by
"row" / "rows", e.g. "rows 3 and 7", "row 42", "3, 5, 7").

If no row numbers are found and "auto" was not said, ask:
> "Which sheet row numbers should I fill forms for?"

### Step 1 — Resolve rows → URLs + docs

Run:
```
cd <project-root>
python scripts/get_apply_urls.py <row1> [row2] ...
```

Parse the JSON output. Each entry has:
- `row` — row number
- `company`, `position` — for display
- `url` — apply URL (may be empty if not yet recorded in sheet)
- `folder` — path to the job folder in the Resume repo (empty if folder not found)
- `resume` — full path to `*_Resume.docx` in that folder (empty if not generated yet)
- `cover_letter` — full path to `*_Cover_Letter.docx` (empty if not generated yet)

For any entry with `"error": "not found"`, warn and skip:
> "Row N not found in Google Sheets — skipping."

For any entry with an empty `url`, warn and skip:
> "Row N (Company – Position) has no apply URL in the sheet — skipping."

If `resume` or `cover_letter` is empty, note it in the summary but continue —
the skill will skip that file upload.

### Step 2 — Read applicant info

Read the file now (once — reuse its contents for every form):
```
Read: <project-root>/applicant_info.txt
```

### Step 3 — Open and fill each form

For each row that has a valid apply URL (in order):

**3a. Open a new browser tab**
```
mcp__chrome-devtools__new_page
```
Note the returned page ID — use it for all subsequent calls for this tab.

**3b. Navigate to the form**
```
mcp__chrome-devtools__navigate_page  →  <apply_url>
```
Wait 2 seconds after navigation, then take a snapshot to confirm the page loaded.
If navigation fails or the page is blank, warn the user and skip to the next row.

**3c. Fill the form (repeat for each page)**

For each page:
  a. `mcp__chrome-devtools__take_snapshot` — see all fields
  b. Fill every field answerable from `applicant_info.txt`:
     - Text / textarea → `mcp__chrome-devtools__fill`
     - Select / dropdown → `mcp__chrome-devtools__fill` with the exact option value.
       First use `evaluate_script` to read all option texts from the open listbox,
       then pick the closest match to the applicant's value.
       Example: "She/Her" in info → prefer "She/her/hers" over "She/her/hers, they/them/theirs"
     - Checkbox → `mcp__chrome-devtools__fill` with `"true"` or `"false"`
     - Radio → `mcp__chrome-devtools__fill` `"true"` on the correct option
     - File input — see 3c-upload below
  c. **File upload fields:**
     - Resume / CV field → upload the `resume` path from Step 1 (skip if empty)
     - Cover letter field → upload the `cover_letter` path from Step 1 (skip if empty)
     - Use `mcp__chrome-devtools__upload_file` for these
  d. Skip any field not answerable from `applicant_info.txt` or the doc paths
  e. Take a fresh snapshot to verify fills

**Dropdown navigation tip:** After clicking a combobox to open it, use:
```javascript
() => {
  const listbox = document.querySelector('[role="listbox"]');
  return Array.from(listbox.querySelectorAll('[role="option"]'))
    .map((o, i) => `${i}: ${o.textContent.trim()}`);
}
```
via `mcp__chrome-devtools__evaluate_script` to read the exact option list before
pressing ArrowDown/Enter. This avoids selecting the wrong option.

**3d. Multi-page navigation**

If a Next / Continue button is visible (but NOT Submit / Apply / Send):
```
mcp__chrome-devtools__click  →  Next button
```
Wait 2 seconds, then repeat Step 3c. Continue up to 20 pages.

**3e. Stop before Submit**

When Submit / Apply / Send Application appears — **STOP. Do NOT click it.**
Scroll to the bottom so all fields are visible.

**3f. Record the fill (non-fatal)**

After stopping before submit, run:
```
cd <project-root>
python scripts/update_filled_forms.py <row> "<company>" "<position>"
```
This records the fill in `data/filled_forms.json` for the midnight daily summary.
If this command fails, ignore the error and continue.

After finishing each tab, report a summary:
```
Tab N — Company: Acme Corp | Position: SWE Intern | Row: 42
  Filled:
    Name: Jane Doe
    Email: janedoe@example.com
    Resume: uploaded (42_Acme_Resume.docx)
    Cover letter: uploaded (42_Acme_Cover_Letter.docx)
    ...
  Skipped (not in applicant_info.txt):
    Cover letter text box
  Status: Ready for review — Submit NOT clicked ✓
```

### Step 4 — Wait for user

After **all** tabs are filled and stopped before submit, report:

```
All N form(s) are filled and waiting in your browser tabs:
  Tab 1 — Acme Corp / SWE Intern (row 42)  →  <url>
  Tab 2 — Initech / Data Analyst (row 7)   →  <url>

Review each tab, make any adjustments, and click Submit when ready.
When you're finished, say "done" (or "close tabs") and I'll close the tabs.
```

Then stop and wait for the user to reply.

### Step 5 — Close tabs (on user confirmation)

When the user says they're done (any of: "done", "submitted", "close", "close tabs",
"all good", "finished"):

For each tab opened in Step 3a:
```
mcp__chrome-devtools__close_page  →  <page_id>
```

Confirm: "Closed N tab(s). All done!"

---

## Error handling

| Situation | Action |
|---|---|
| Row not in sheet | Warn user, skip (normal) / status `no_url` (auto) |
| Row has no apply URL | Warn user, skip (normal) / status `no_url` (auto) |
| `get_apply_urls.py` fails | Show error output, ask user to check sheet auth |
| No job folder found | Note in summary, continue without file upload |
| Resume/cover letter not generated yet | Note in summary, skip that upload |
| Page not found / job gone | Skip (normal) / status `no_longer_exists` + pause if last (auto) |
| Sign-in wall after navigation | Report explicitly, leave tab open, status `login_required` |
| Field type unclear | Use `evaluate_script` to inspect DOM, then fill or skip |
| Navigation takes >15 s | Warn user, continue anyway |
| Discord webhook unavailable | Print summary in conversation instead |

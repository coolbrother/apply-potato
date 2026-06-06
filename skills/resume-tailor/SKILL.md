---
name: resume-tailor
description: Tailors an existing .docx resume for a specific job posting. Use this skill whenever the user provides a job URL or job description and wants a targeted version of their resume. Produces a separate output file (never modifies the base resume). Covers: fetching and analyzing the posting, gap analysis, reframing bullets, reordering skills, adding implicit skills, and repacking the docx.
---

# Resume Tailor

Produces a job-targeted copy of a `.docx` resume. Three phases: **Analyze → Plan → Apply**.

The XML editing mechanics come from `document-skills:docx` and the file workflow from `document-skills:resume-updater` — both are assumed loaded.

---

## Sandbox rule

**You are only permitted to read and write files under the resume output directory configured in `.env` (`JOB_DESC_OUTPUT_DIR`).**
Never access project code, shell history, credentials, or any path outside that directory.
Never run `find`, `ls`, `dir`, `Get-ChildItem`, `Glob`, or any filesystem search.

---

## Source resume constraint

**Use ONLY the resume file explicitly provided in the prompt.** The path is always given — do
not search for it. Do NOT open any `.docx` file directly; the XML is pre-extracted and the
path to it is in the prompt.

---

## Automated pipeline mode

When the prompt argument is a **folder path** (e.g. `$JOB_DESC_OUTPUT_DIR/3013_Pathos`),
this skill is being invoked by the automated Phase 2 pipeline. In this mode:

- Read the job description from `{folder}/{stem}_job_desc.md` (path given in prompt) — no WebFetch
- The resume XML is pre-unpacked; its exact path is given in the prompt
- No user is present — **skip Phase 2 confirmation** and proceed directly to applying edits
- Do NOT repack — Python handles repacking automatically after this skill exits
- Output goes to the folder specified; naming is handled by the pipeline

---

## Phase 1: Analyze

### 1a. Get the job description

**Automated pipeline:** Read the `_job_desc.md` file at the path given in the prompt.

**Manual/URL mode:** Use `WebFetch` on the provided URL. Extract:
- Job title and team/department
- Required qualifications (languages, tools, domains)
- Preferred qualifications — these are the most actionable; exact phrasing matters
- Key responsibilities (surface tech/domain keywords)

### 1b. Read the current resume

Unpack if not already unpacked (see `resume-updater` Apply phase for the unpack command). Extract:
- Technical Skills section: all three bullets (Languages, Frameworks/Libraries, Developer Tools)
- All bullet text across Work Experience and Project Experience

### 1c. Build a gap analysis table

| Job requirement | Resume coverage | Action |
|---|---|---|
| Each required/preferred item | Present / partial / absent | Add / reframe / reorder / skip |

**Action rules:**
- **Add**: skill is on the resume (even implicitly in a bullet) but missing from the skills section
- **Reframe**: a bullet's content maps to a preferred qual but uses different language — rephrase to match the posting's exact terminology
- **Reorder**: a relevant skill is already listed but buried — move it to the front of its line
- **Skip**: skill is not on the resume and user doesn't have it — never invent experience

Always confirm with the user before adding any language, tool, or framework not currently on the resume.

---

## Phase 2: Plan

Present the proposed changes to the user before touching any files:

1. **New output filename**: `OriginalName_Company.docx` (e.g., `YourNameResume_Cloudflare.docx`)
2. **Skills section changes**: which items to add, which to reorder, and to what position
3. **Bullet reframes**: show current vs. proposed text for each changed bullet
4. **Skipped items**: list what the job asks for that isn't being added, and why

Do not proceed to Apply until the user confirms (or adjusts) the plan.

---

## Phase 3: Apply

### Step 1: Copy the unpacked directory

**Never edit `resume_unpacked` directly** — it is the source of truth for the base resume.

```bash
cp -r "C:/path/to/resume_unpacked" "C:/path/to/resume_unpacked_Company"
```

All edits go into the `_Company` copy. After repacking (Step 3), delete both temp directories:

```bash
rm -rf "C:/path/to/resume_unpacked"
rm -rf "C:/path/to/resume_unpacked_Company"
```

### Step 2: Edit XML

Use the **Edit tool** for all changes. Follow the XML patterns in `document-skills:resume-updater` (inline bold runs, tab stops, etc.).

#### Skills list reordering

Skills lines are plain text runs. Find the run containing the comma-separated list and rewrite it with job-relevant items first:

```xml
<!-- Before -->
<w:t> Java, Python, C, C++, JavaScript, ...</w:t>

<!-- After — Python and JavaScript pulled to front for a Python/JS role -->
<w:t> Python, JavaScript, Java, C, C++, ...</w:t>
```

#### Adding a skill to a list

Append to the relevant run's text. Prefer the most specific category (e.g., PyTorch → Frameworks/Libraries, not Developer Tools):

```xml
<w:t>NumPy, Pandas, Matplotlib, scikit-learn, PyTorch</w:t>
```

#### Bullet reframe

Replace the full set of `<w:r>` runs in the bullet paragraph. Keep the paragraph's `<w:pPr>` (list numbering, spacing) unchanged — only swap the run content.

Bold key terms using separate `<w:r>` runs with `<w:b/><w:bCs/>`. Normal text runs have neither.

Use the job posting's **exact preferred-qualification language** as the headline of the bullet. For example, if the posting says "AI agents" and "LLM evaluations", open the bullet with those phrases:

> Architected a self-hosted **AI agent** pipeline … designed **LLM evaluation** and prompt optimization workflows …

### Step 3: Repack

```python
import zipfile, os, shutil
src = 'C:/path/to/resume_unpacked_Company'
out = 'C:/path/to/YourNameResume_Company.docx'
tmp = out + '.tmp'
with zipfile.ZipFile(tmp, 'w', zipfile.ZIP_DEFLATED) as zf:
    for root, dirs, files in os.walk(src):
        for file in files:
            filepath = os.path.join(root, file)
            arcname = os.path.relpath(filepath, src).replace(os.sep, '/')
            zf.write(filepath, arcname)
shutil.move(tmp, out)
```

Use `python` (not `python3`) on this machine. Do not use `pack.py` (Python 3.10+ only).

---


## Pitfall summary

| # | Pitfall | Fix |
|---|---------|-----|
| A | Modifying the base resume | Always copy `resume_unpacked` → `resume_unpacked_Company` first; never edit the original |
| B | Skill used in a bullet but absent from skills section | Check each bullet for tech not in the skills lists — these are easy adds |
| C | Adding skills the user doesn't have | Confirm before adding any language/tool not currently on the resume |
| D | Reframe misses the posting's exact language | Quote the preferred qualification verbatim in the bullet headline |
| E | Reordering only one skills line | Check all three lines (Languages, Frameworks, Developer Tools) against the posting |
| F | `python3` not found on Windows | Use `python` instead |
| G | Editing `resume_unpacked` instead of the copy | Double-check the file path before every Edit call |
| H | Searching the filesystem for a resume | Use ONLY the file path given in the prompt — never run find/ls/Glob to locate one |

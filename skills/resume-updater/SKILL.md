---
name: resume-updater
description: Updates a .docx resume with new work or project experiences. Use this skill whenever the user mentions a new job, internship, research position, course project, club project, or TA role they want to add to their resume — even if they are unsure it belongs. Also use it when the user wants to swap, remove, or update any existing resume entry, change a job title, or update dates. This skill handles the full workflow: deciding whether the change is warranted, drafting strong resume content, and editing the .docx file directly. Trigger it any time a resume and a new experience appear together in the same request.
---

# Resume Updater

Edits a `.docx` resume to incorporate a new experience. Three phases: **Assess → Draft → Apply**.

The XML editing mechanics come from `document-skills:docx` — that skill is already loaded when this one runs, so refer to its XML reference section if you need patterns not covered here.

---

## Phase 1: Assess

Read the resume before writing anything. Unpack the .docx (see Apply phase, Step 1) and extract all current entries from `word/document.xml` so you have the full picture.

### Ask what you need to know

If the user hasn't told you, ask:
- What did they build / do / accomplish? (technologies, outcomes, metrics)
- What dates?
- Is there a public URL (GitHub, website)? Do not assume — course projects at universities like Cornell **cannot** be made public due to academic integrity policies. Never invent or guess a URL.
- What is the resume targeting? (SWE, research, quant, etc.)

### Make the call

For each section (Work Experience, Project Experience), decide: **add, substitute, or skip**.

Factors that make an entry stronger:
- **Technical depth** — systems work, novel algorithms, compilers, full stack > toy ML exercise, CRUD app
- **Skill diversity** — introduces a language or framework not yet on the resume (high value)
- **Credibility signal** — research lab, recognized org (e.g., CDS), course with known rigor
- **Team collaboration** — team projects alongside solo projects add a different kind of signal
- **Recency** — newer work generally displaces older work of equal strength

If substituting, name the entry being replaced and explain the tradeoff in one sentence. The user may disagree — that's fine. **Do not proceed to Draft until the user confirms.**

Also flag: does this experience add any new languages or frameworks that should go in Technical Skills?

---

## Phase 2: Draft

Write content that matches the existing resume's style before touching any files.

### Header line

```
[Bold] "Project/Org Name | "   [Italic] "Role"   [Bold] " (GitHub)"   [tab]   "Mon YYYY – Mon YYYY"
```

- Only include ` (GitHub)` if the user explicitly provides a public URL
- Omit the link entirely if none — no empty `()` placeholder
- Use `Present` for ongoing roles (e.g., `January 2026 – Present`)

### Bullet

- **One strong action verb** to open (Built, Designed, Implemented, Led, Engineered)
- **Bold** key technologies and numeric metrics inline (see XML pattern below)
- **1–2 sentences**, dense with signal, matching the existing bullets' length
- Use `&#x2019;` for apostrophes, `&#x2013;` for en-dashes in date ranges, `&#x201C;`/`&#x201D;` for double quotes

**Good example:**
> Designed and implemented a full-pipeline compiler for the Eta language in **Java** with a 4-person team, spanning lexical analysis, recursive descent parsing, type checking, and **x86-64** code generation; extended the compiler to support Rho, an augmented superset of Eta.

---

## Phase 3: Apply

### Step 1: Unpack

**Always unpack to a path inside the resume's own directory** — never to `/tmp/`. The Read and Edit tools operate on Windows paths and cannot see Linux filesystem paths.

```bash
SKILL_DOCX="C:/Users/.../.claude/plugins/cache/anthropic-agent-skills/document-skills/.../skills/docx"
python "$SKILL_DOCX/scripts/office/unpack.py" "C:/path/to/resume.docx" "C:/path/to/resume_unpacked/"
```

### Step 2: Edit XML

Use the **Edit tool directly** for all changes. Never write a Python script to edit XML — the Edit tool shows exactly what changes and is safer.

#### Header paragraph structure

Every project/work header paragraph **must** have a right-aligned tab stop declared in `<w:pPr>`. Without it, the date floats left instead of hugging the right margin. Add it before `<w:spacing>`:

```xml
<w:pPr>
  <w:tabs>
    <w:tab w:val="right" w:pos="10800"/>
  </w:tabs>
  <w:spacing w:after="0"/>
  <w:rPr>
    <w:rFonts w:ascii="Times New Roman" w:eastAsia="Times New Roman"
              w:hAnsi="Times New Roman" w:cs="Times New Roman"/>
  </w:rPr>
</w:pPr>
```

Place exactly **one** `<w:tab/>` immediately before the date text (inside the last run of the paragraph). Multiple tabs or spaces before the date = misaligned. One tab + the right tab stop = perfect.

```xml
... <w:tab/><w:t>January 2026 – May 2026</w:t></w:r>
```

#### Inline bold for technology names

Split into separate `<w:r>` runs at each bolded term. Normal text has no `<w:b/>`, bold text has both `<w:b/>` and `<w:bCs/>`:

```xml
<w:r>
  <w:rPr><w:rFonts w:ascii="Times New Roman" ... /></w:rPr>
  <w:t xml:space="preserve">Built a </w:t>
</w:r>
<w:r>
  <w:rPr><w:rFonts w:ascii="Times New Roman" ... /><w:b/><w:bCs/></w:rPr>
  <w:t>Rust</w:t>
</w:r>
<w:r>
  <w:rPr><w:rFonts w:ascii="Times New Roman" ... /></w:rPr>
  <w:t xml:space="preserve"> cache simulator...</w:t>
</w:r>
```

Always add `xml:space="preserve"` to `<w:t>` elements that have leading or trailing spaces.

#### Hyperlinks

To add a new GitHub/website link, add a relationship in `word/_rels/document.xml.rels`:

```xml
<Relationship Id="rId99" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink"
  Target="https://github.com/user/repo" TargetMode="External"/>
```

Then reference it in the header: `<w:hyperlink r:id="rId99" w:history="1">...</w:hyperlink>`

**When replacing an entry that had a hyperlink:**
- New entry has a URL → update `Target=` in the rels file (keep the same rId)
- New entry has no URL → remove the `<w:hyperlink>` element **and** its surrounding ` (` and `)` bold runs from document.xml entirely

Never leave a stale rId pointing to the old project's URL — it will silently link to the wrong repo.

### Step 3: Repack

**Do not use `pack.py`** — it uses Python 3.10+ type union syntax (`str | None`) that fails on Python 3.9. Use the bundled script instead:

```bash
python scripts/repack_docx.py "C:/path/to/resume_unpacked" "C:/path/to/resume.docx"
```

Or inline:

```python
import zipfile, os, shutil
src = 'C:/path/to/resume_unpacked'
out = 'C:/path/to/resume.docx'
tmp = out + '.tmp'
with zipfile.ZipFile(tmp, 'w', zipfile.ZIP_DEFLATED) as zf:
    for root, dirs, files in os.walk(src):
        for file in files:
            filepath = os.path.join(root, file)
            arcname  = os.path.relpath(filepath, src).replace(os.sep, '/')
            zf.write(filepath, arcname)
shutil.move(tmp, out)
```

### Step 4: Update Technical Skills (if needed)

If the new experience introduces a language or framework not already listed, add it to the appropriate bullet in the Technical Skills section using the same Edit-tool approach.

---

## Pitfall summary

| # | Pitfall | Fix |
|---|---------|-----|
| A | `pack.py` crashes on Python 3.9 | Use `scripts/repack_docx.py` or inline zipfile code |
| B | Date not right-aligned | Add `<w:tabs><w:tab w:val="right" w:pos="10800"/></w:tabs>` to `<w:pPr>`; use exactly one `<w:tab/>` before date |
| C | Unpacked files not visible to Edit tool | Unpack inside the resume directory, not `/tmp/` |
| D | Course project GitHub link | Never add one unless user provides a confirmed public URL |
| E | Stale hyperlink after replacement | Update rId target URL, or remove the `<w:hyperlink>` + `()` runs entirely |
| F | Quotes/dashes look wrong | Use `&#x2019;` `&#x201C;` `&#x201D;` `&#x2013;` entities |
| G | Bold text not rendering | Bold needs both `<w:b/>` and `<w:bCs/>` in a separate `<w:r>` run |

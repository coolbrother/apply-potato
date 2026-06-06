---
name: cover-letter
description: >
  Generates a tailored cover letter as a .docx file for a specific job posting.
  Use this skill whenever the user provides a job URL or job description and wants
  a cover letter written — even if they just say "write me a cover letter for this"
  or paste a job posting. Also use it when the user says things like "apply to this
  job", "draft a cover letter", or "make a cover letter for [company/role]".
  The skill reads the user's resume from the current directory, analyzes the job
  requirements, and produces a CoverLetter_[Company].docx that sounds like a real
  person wrote it — not a template.
---

# Cover Letter Generator

Generate a tailored cover letter `.docx` for a job posting. The goal is a letter that reads
like it was written by a thoughtful person who actually read the job description — not assembled
from cover letter templates.

## Sandbox rule

**You are only permitted to read and write files under the Resume output directory configured in `.env` (`JOB_DESC_OUTPUT_DIR`).**
Never access project code, shell history, credentials, or any path outside that directory.
Never run `find`, `ls`, `dir`, `Get-ChildItem`, or any filesystem search outside that directory.

## Automated pipeline mode

When the prompt argument is a **folder path** (e.g. `/Path/To/Your/Resume/3013_Pathos`),
this skill is being invoked by the automated Phase 2 pipeline. In this mode:

- The job description path and resume XML path are given explicitly in the prompt — read them directly
- No user is present — skip any confirmation steps and generate immediately
- Write the JS script to the job folder (path given in prompt), not the project root
- Output filename is `{stem}_Cover_Letter.docx` written to the job folder (exact path in prompt)

## Step 1: Gather inputs

**Job posting:**
- **Automated pipeline:** Read the `_job_desc.md` file at the path given in the prompt — no WebFetch.
- **Manual/URL mode:** If the user gave a URL, fetch it. If they pasted a description, parse it directly.

**Resume:** The prompt specifies a pre-unpacked XML path — use it directly. Read `word/document.xml`
from the path given. Do NOT search for or open any `.docx` file. Do NOT run `find`, `ls`, `Glob`,
or any command to locate a resume. If no path is provided, ask the user.

Extract: name, contact info (phone, email, city), education, work experience with bullet
details, projects with bullet details, and technical skills.

## Step 2: Analyze and draft

Read the job description carefully. Identify:
- What problem is this team actually solving?
- What's technically interesting or unusual about this company's scale/context?
- Which 2–3 experiences from the resume are most relevant?

Then draft 3–4 paragraphs:

**Opening (1 paragraph):** State the role and team. Then make one specific, true observation
about why this company's technical context matters — not flattery, an actual reason the
problem is interesting. End with a sentence establishing who the applicant is (school, major,
GPA if strong, relevant experience category).

**Body (2 paragraphs):** Lead with the most relevant project or experience. Name it, say what
it did, describe the part that was technically demanding. Connect it to what the team does
without using connector phrases — just put the relevant work next to the relevant responsibility
and let the reader make the connection. Cover infrastructure, ML, or systems experience in one
paragraph; cross-disciplinary or collaborative experience in the other.

**Closing (1 short paragraph):** 1–2 sentences. State directly what you want to do there.
End with something like "If my background looks like a fit, I'd be glad to talk." — not
"Thank you for your consideration."

### Writing rules — read these carefully

The letter fails if it sounds like a template. These phrases are banned:

| Never write | Why / what to write instead |
|---|---|
| "excited/thrilled/passionate about" | State the reason directly |
| "caught my attention" | Say what specifically about the role |
| "intersection of X and Y" | Describe the actual overlap in concrete terms |
| "I'm drawn to [anything]" | Cut entirely — say what the work is, not that you're drawn to it |
| "maps directly to" / "aligns perfectly with" | Let the evidence speak; or say "that's roughly the same problem" |
| "exactly the kind of environment I thrive in" | Cut it entirely |
| "I'd welcome the opportunity" | End directly: "I'd be glad to talk." |
| "Thank you for your consideration" | Cut or replace with something specific |
| "hands-on experience" | Just "experience" or describe it directly |
| "meaningful [fraction/impact]" | Use a specific number or cut the adjective |
| "unique opportunity" / "fast-paced" / "I believe" | Cut |
| "I find X genuinely compelling" | Show why through specifics instead |
| "actually" as emphasis | Cut |
| "That's the [kind of / type of] [X] I want to work on" and variants | Cut — state what you'd do there instead |
| "[doing X] meant [thinking through / reasoning about] A, B, and C — not just Y" | This structure is a tell. Rewrite as a direct claim about what the work required, without the "not just" contrast |

Filler adjectives to avoid entirely: unique, meaningful, incredible, amazing, passionate, excited.

**Coursework is not a body paragraph.** A paragraph built around "my coursework in X has prepared me for Y" is a template paragraph — cut it. The body paragraphs must be anchored by specific projects or work experience, not classes. Mention a course only as a one-clause aside inside a paragraph that's already carrying concrete evidence ("...which maps to what I was doing in my distributed systems course" — fine as a clause, not as the paragraph's reason for existing).

## Step 3: Generate the .docx

Node.js with the `docx` package is the generation tool. Python's python-docx is **not** used.

**Find node:** Run `node --version` first. If that fails, try `which node` on macOS/Linux
or `where node` on Windows to locate it.

**Find the docx package:** It lives in the skill's own directory, not the working directory.
Use `<skill-base-dir>/node_modules/docx`. If `node_modules/` is missing (it is gitignored —
run `npm install` inside `<skill-base-dir>` to restore it), or from the user's home directory
if npm is blocked in the current working directory.

**Write a temp script** (`_cover_letter_gen.js`) in the working directory, run it, then delete it.

### Document structure

```javascript
const { Document, Packer, Paragraph, TextRun, AlignmentType } = require('<skill-base-dir>/node_modules/docx');
const fs = require('fs');

const TNR = "Times New Roman";
const font = (text, opts = {}) => new TextRun({ text, font: TNR, size: 24, ...opts });
const para = (children, opts = {}) => new Paragraph({
  children: Array.isArray(children) ? children : [font(children)],
  spacing: { after: 0, before: 0 },
  ...opts
});
const spacer = () => new Paragraph({ children: [font("")], spacing: { after: 0, before: 0 } });
```

**Layout:**
- US Letter: `size: { width: 12240, height: 15840 }`, margins `1080` DXA on all sides (~0.75")
- Name centered, size 28, **not bold**
- Contact line centered, size 22: `phone ● email ● city`
- Two `spacer()` calls, then the date written out (e.g. `"May 22, 2026"`)
- Recipient block: company name, address if known
- Blank line, salutation: `"Dear [Company] Recruiting Team,"`
- Blank line between every paragraph
- Closing: `"Sincerely,"` on one line, name on the **very next line** — no spacer between them
- **Nothing bolded anywhere** in the letter

```javascript
// Closing — name touches "Sincerely," with no gap
para("Sincerely,"),
para(name),   // NOT: spacer(), spacer(), para([font(name, { bold: true })])
```

**Run and clean up:**
```bash
node _cover_letter_gen.js   # or full path to node
rm _cover_letter_gen.js     # always delete — even if node errored
```

## Step 4: Output

**Automated pipeline:** Save to the exact path given in the prompt (`{stem}_Cover_Letter.docx`
in the job folder). Report the filename and path.

**Manual mode:** Save as `CoverLetter_[CompanyName].docx` in the current working directory.

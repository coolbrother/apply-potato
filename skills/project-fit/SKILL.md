---
name: project-fit
description: >
  Analyzes how a project fits one or more job postings and maintains a persistent fit report
  in Documents\Resume. Takes a job URL or pasted description plus a project (defaults to the
  current working directory), then produces or updates a three-section markdown report:
  (1) what companies are looking for — accumulated and thematically grouped across every job
  analyzed so far for this project; (2) where the project matches those requirements, with
  specific module/file-level evidence; (3) gaps to close and skills to learn, prioritized by
  how often each appears across all analyzed jobs. Use this skill whenever the user says things
  like "analyze how my project fits this job", "add this job to my fit report", "what does this
  job want that my project is missing", "update the fit report for [project]", or pastes a job
  and asks for a gap analysis against their work.
---

# project-fit

Produce or update a persistent fit report showing how a project relates to one or more job
postings. The report accumulates job requirements over time so it stays representative as more
jobs are analyzed.

## Report location

`C:\Users\[username]\Documents\Resume\[ProjectName]_fit_report.md`

Resolve `[username]` from the environment (`$env:USERNAME` on Windows, `$USER` on Unix).

**Naming scheme:** Normalize `[ProjectName]` consistently:
- Lowercase only
- Replace spaces with hyphens
- Strip special characters (keep alphanumeric and hyphens)
- Examples: "Apply Potato" → `apply-potato`, "My App v2!" → `my-app-v2`

The full filename is always `[normalized-name]_fit_report.md`.

## Step 1: Parse inputs

**Job:** look for a `http://` or `https://` URL, or a pasted job description in the user's
message. If neither is present, ask: "Please provide a job URL or paste the job description."

**Project name:** look for explicit naming like "for apply-potato" or "for my [name] project".
If not stated, use the basename of the current working directory and confirm:
"I'll use '[dirname]' as the project name — correct?"

## Step 2: Get the job description

The prompt will always contain a job URL. It may also contain pre-scraped `<page_content>` — if so, use that instead of fetching.

- **If `<page_content>...</page_content>` is present in the prompt:** use that text directly. Do not fetch the URL. The `Job URL:` line above it is metadata only (for the report).
- **If only a URL is present:** fetch it with WebFetch. If the page returns a 403 or empty body, ask the user to paste the description instead.
- **If pasted text is present:** parse it directly.

Extract: company name, job title, and the content of every requirements-like section (responsibilities, requirements, nice-to-have / bonus, etc.).

Identify and label these three areas even if the posting uses different section names:
- **What they're building / responsibilities** — what the role actually does
- **Hard requirements** — must-haves
- **Bonus / nice-to-have** — preferred but not required

**Discard personal eligibility requirements** — anything that is about the candidate as a person rather than what they've built: enrollment status, graduation year, work authorization, GPA minimums, visa sponsorship, degree type. These cannot be demonstrated through a project and have no place in the report.

## Step 2.5: AI relevance check

Before proceeding, determine whether this job is AI-relevant. A job qualifies if any of the following appear in the title, responsibilities, or requirements:

- AI, ML, machine learning, deep learning, LLM, NLP, computer vision
- Agent, agentic, autonomous systems, RAG, embeddings, vector search
- Data science, applied science, AI infrastructure, AI platform
- Roles explicitly building with or on top of AI/ML models (AI engineer, ML engineer, AI product, AI tools)

**If the job is not AI-relevant:** stop here. Do not update the report. Tell the user:
"Skipping — this job doesn't appear to be AI-relevant. The fit report only tracks AI/ML roles."

**If the job is AI-relevant:** continue to Step 3.

## Step 3: Understand the project

Read from the current working directory in this priority order:
1. `CLAUDE.md` — most authoritative; describes architecture, modules, and tech stack
2. `README.md` — user-facing description
3. If neither exists: read the top-level entry point files (`.py`, `.ts`, `.js`) to infer purpose

Build a model of:
- What the project does and why
- Technologies, frameworks, and APIs used
- Notable engineering patterns (multi-step pipelines, LLM integration, API integrations, etc.)
- What's absent or thin

Do NOT re-read files you've already read in this session if they're still in context.

## Step 4: Read the existing report

Check if `C:\Users\[username]\Documents\Resume\[ProjectName]_fit_report.md` exists.

- **Exists:** read it. Extract the Jobs Analyzed table (company, title, date) and the full
  Section 1 consolidated requirements. Note what's already covered to avoid duplication.
- **Does not exist:** start fresh; N = 0.

If `Documents\Resume\` doesn't exist, create it.

## Step 5: Analyze

Combine the existing accumulated requirements with this new job's requirements. Then evaluate
the project against the complete picture (not just the new job alone).

**For each requirement theme, assess:**
- What concrete evidence in this project satisfies it? (Be specific: name modules, patterns,
  what the code actually does. Mention file paths when useful.)
- Is it a strong match, partial match, or gap?

**For gaps:**
- How many jobs have asked for this? (frequency matters — frequent gaps rank higher)
- Is there a specific feature addition or refactor that would close the gap?
- Is there an external framework or tool to learn?

## Step 6: Write the updated report

Write the full report to the path resolved in Step 4. Overwrite the existing file entirely —
do not append; the whole document is regenerated from scratch each run with the new job merged in.

### Report structure

```
# Project Fit Report: [ProjectName]

_Updated: [today's date] | Jobs analyzed: [N]_

---

## What Companies Are Looking For

### Jobs Analyzed

| # | Company | Title | Analyzed |
|---|---------|-------|----------|
| 1 | Acme Corp | Software Intern | 2026-05-01 |
| 2 | Scale AI | AI Builder Intern | 2026-06-06 |

### Consolidated Requirements

Group by theme. Merge identical or near-identical requirements from different jobs.
After each requirement, cite which jobs mention it in brackets, e.g. `[Scale AI, Stripe]`.

**[Theme Name]**
- Requirement ... [Company A]
- Requirement ... [Company A, Company B]

List as many themes as the data warrants.

---

## Where [ProjectName] Matches

For each requirement theme from Section 1:

### [Theme Name]
**[Strong match / Partial match / Not covered]**

Evidence: [specific modules, patterns, API integrations, code behaviour — concrete, not vague].
If partial, state clearly what's there and what's absent.

---

## Gaps & Skills to Learn

### Critical Gaps
Requirements that appear in multiple jobs and are absent or weak in the project. List in
descending order of frequency. For each:
- **[Gap name]** — what's missing and why it matters. Frequency: [N jobs].

### Feature Opportunities
Specific additions to the project that would close the gaps above. For each:
- **[Feature name]** — what to build and how it maps to the gap.

### Skills to Learn
External frameworks, tools, or patterns to pick up. For each:
- **[Skill / Framework]** — why it matters for this job category and how to apply it here.
```

Keep the report factual and specific. Avoid vague phrases like "demonstrates strong alignment"
or "shows familiarity with". Name the actual evidence or don't claim the match.

## Step 7: Confirm to user

After saving, tell the user:
- Full path where the report was saved
- How many jobs are now in the report (including this one)
- The top 2–3 gaps (so they get the key finding without opening the file)

Example:
```
Report saved: C:\Users\sz\Documents\Resume\apply-potato_fit_report.md
Jobs analyzed: 1 (Scale AI — AI Builder Intern)

Top gaps:
1. No named orchestration framework (LangChain/LangGraph/CrewAI) — mentioned by 1 job
2. No Slack integration — mentioned by 1 job
3. No web dashboard or UI — mentioned by 1 job
```

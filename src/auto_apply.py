"""
Auto-apply orchestrator for ApplyPotato.

For Dream Company / salary-threshold jobs:
  1. Detect whether the apply form needs a resume and/or cover letter
     (text-based AI pass, optionally followed by screenshot-based vision pass)
  2. Record findings in Google Sheets Notes column
  3. If AUTO_APPLY_ENABLED=true, generate tailored documents via claude CLI
     or openai/gemini API
"""

import asyncio
import base64
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from typing import Optional

from .config import Config, get_config
from .ai_extractor import ExtractedJob

logger = logging.getLogger(__name__)

def _sanitize_name(s: str, max_len: int = 50) -> str:
    """Sanitize a string for use as a folder/file name component."""
    s = re.sub(r'[^\w\s-]', '', s).strip()
    s = re.sub(r'[\s]+', '_', s)
    return s[:max_len]



class AutoApplyOrchestrator:
    """Detects apply requirements and (optionally) generates application documents."""

    def __init__(self, config: Optional[Config] = None):
        self.config = config or get_config()
        self._prompt_template: Optional[str] = None

    def _load_prompt(self) -> str:
        if self._prompt_template is None:
            prompt_path = self.config.prompts_dir / "requirement_detection.txt"
            self._prompt_template = prompt_path.read_text(encoding="utf-8")
        return self._prompt_template

    # ------------------------------------------------------------------
    # Security helpers: snapshot, identity check, pre/post docx processing
    # ------------------------------------------------------------------

    def _snapshot_files(self, root: Path, exclude_dirs: set) -> dict:
        """Return path -> mtime for all files under root, skipping excluded dirs."""
        snapshot = {}
        try:
            for f in root.rglob('*'):
                if not f.is_file():
                    continue
                if any(f == d or (d.exists() and f.is_relative_to(d)) for d in exclude_dirs):
                    continue
                try:
                    snapshot[str(f)] = f.stat().st_mtime
                except OSError:
                    pass
        except Exception as e:
            logger.debug(f"Snapshot error: {e}")
        return snapshot

    def _check_snapshot(self, before: dict, after: dict, output_dir: Path) -> None:
        """Warn about unexpected file changes outside output_dir."""
        output_prefix = str(output_dir)
        new_files = {k for k in set(after) - set(before) if not k.startswith(output_prefix)}
        modified = {
            k for k in set(before) & set(after)
            if after[k] != before[k] and not k.startswith(output_prefix)
        }
        if new_files:
            logger.warning(
                f"SECURITY: skill wrote files outside output dir: {', '.join(sorted(new_files))}"
            )
        if modified:
            logger.warning(
                f"SECURITY: skill modified files outside output dir: {', '.join(sorted(modified))}"
            )

    def _check_identity(self, resume_path: Path, output_path: Path) -> bool:
        """Verify the generated resume contains the same person's name as the original."""
        def _extract_name(docx_path: Path) -> Optional[str]:
            """Return the first 1-4 words of the first non-trivial text run (the person's name)."""
            try:
                with zipfile.ZipFile(str(docx_path)) as z:
                    with z.open('word/document.xml') as f:
                        raw = f.read().decode('utf-8', errors='replace')
                # Strip XML tags, collapse whitespace, split into tokens
                text = re.sub(r'<[^>]+>', ' ', raw)
                words = text.split()
                # Skip leading junk (single chars, numbers, punctuation)
                for i, w in enumerate(words):
                    if len(w) > 2 and w.isalpha():
                        # Take up to 3 consecutive alpha words as the name
                        name_words = []
                        for w2 in words[i:i + 3]:
                            if w2.isalpha() or (len(w2) > 1 and w2[:-1].isalpha()):
                                name_words.append(w2.strip('.,'))
                            else:
                                break
                        if name_words:
                            return ' '.join(name_words)
            except Exception:
                pass
            return None

        original_name = _extract_name(resume_path)
        if not original_name:
            logger.debug("Identity check: could not extract name from original resume")
            return True

        try:
            with zipfile.ZipFile(str(output_path)) as z:
                with z.open('word/document.xml') as f:
                    output_text = re.sub(
                        r'<[^>]+>', ' ', f.read().decode('utf-8', errors='replace')
                    )
            if original_name.lower() in output_text.lower():
                return True
            logger.warning(
                f"SECURITY: identity check failed — '{original_name}' not found in output resume"
            )
            return False
        except Exception as e:
            logger.debug(f"Identity check error: {e}")
            return True

    def _preprocess_resume(
        self, resume_path: Path, work_dir: Path, company_tag: str
    ) -> tuple:
        """
        Unpack resume.docx and create a company-specific copy ready for editing.
        Returns (unpacked_base, unpacked_company).
        """
        unpacked_base = work_dir / "resume_unpacked"
        unpacked_company = work_dir / f"resume_unpacked_{company_tag}"

        for d in [unpacked_base, unpacked_company]:
            if d.exists():
                shutil.rmtree(str(d))

        unpacked_base.mkdir(parents=True)
        with zipfile.ZipFile(str(resume_path)) as z:
            z.extractall(str(unpacked_base))

        shutil.copytree(str(unpacked_base), str(unpacked_company))
        return unpacked_base, unpacked_company

    def _repack_docx(self, unpacked_dir: Path, output_path: Path) -> None:
        """Zip an unpacked docx directory back into a .docx file."""
        tmp = Path(str(output_path) + '.tmp')
        with zipfile.ZipFile(str(tmp), 'w', zipfile.ZIP_DEFLATED) as zf:
            for root, dirs, files in os.walk(str(unpacked_dir)):
                for file in files:
                    filepath = Path(root) / file
                    arcname = filepath.relative_to(unpacked_dir).as_posix()
                    zf.write(str(filepath), arcname)
        shutil.move(str(tmp), str(output_path))

    # ------------------------------------------------------------------
    # AI provider dispatch
    # ------------------------------------------------------------------

    def _call_ai_text(self, prompt: str) -> Optional[dict]:
        """Call the configured AI provider with a text prompt. Returns parsed JSON or None."""
        provider = self.config.auto_apply.provider
        try:
            if provider == "openai":
                return self._call_openai(prompt, screenshot_path=None)
            elif provider == "gemini":
                return self._call_gemini(prompt, screenshot_path=None)
            else:  # "claude" — use CLI, no API key needed
                return self._call_claude_cli_detection(prompt)
        except Exception as e:
            logger.warning(f"AI detection call failed: {e}")
            return None

    def _call_ai_vision(self, prompt: str, screenshot_path: Path) -> Optional[dict]:
        """Call the configured AI provider with prompt + screenshot. Returns parsed JSON or None."""
        provider = self.config.auto_apply.provider
        try:
            if provider == "openai":
                return self._call_openai(prompt, screenshot_path)
            elif provider == "gemini":
                return self._call_gemini(prompt, screenshot_path)
            else:
                # claude provider doesn't support image input via CLI in -p mode;
                # callers should use _call_ai_text with fresh page content instead.
                return self._call_claude_cli_detection(prompt)
        except Exception as e:
            logger.warning(f"AI vision detection call failed: {e}")
            return None

    def _call_openai(self, prompt: str, screenshot_path: Optional[Path]) -> Optional[dict]:
        from openai import OpenAI
        client = OpenAI(api_key=self.config.openai_api_key)

        messages = []
        if screenshot_path:
            img_data = base64.b64encode(screenshot_path.read_bytes()).decode()
            messages.append({
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img_data}"}},
                ],
            })
        else:
            messages.append({"role": "user", "content": prompt})

        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages,
            max_tokens=512,
        )
        raw = response.choices[0].message.content
        logger.info(f"[detection:openai] {raw}")
        return _parse_json_response(raw)

    def _call_gemini(self, prompt: str, screenshot_path: Optional[Path]) -> Optional[dict]:
        from google import genai
        from google.genai import types as genai_types

        client = genai.Client(api_key=self.config.gemini_api_key)
        contents = [prompt]
        if screenshot_path:
            contents.append(
                genai_types.Part.from_bytes(
                    data=screenshot_path.read_bytes(),
                    mime_type="image/png",
                )
            )
        response = client.models.generate_content(
            model="gemini-2.0-flash",
            contents=contents,
        )
        logger.info(f"[detection:gemini] {response.text}")
        return _parse_json_response(response.text)

    def _call_claude_cli_detection(self, prompt: str) -> Optional[dict]:
        """Run a detection prompt via the claude CLI and parse JSON from stdout."""
        project_root = Path(__file__).parent.parent
        try:
            result = subprocess.run(
                ["claude", "-p", prompt],
                capture_output=True,
                text=True,
                cwd=str(project_root),
                timeout=60,
            )
            if result.returncode != 0:
                logger.warning(f"claude CLI detection failed (exit {result.returncode}): stderr={result.stderr[:300]!r} stdout={result.stdout[:300]!r}")
                return None
            logger.info(f"[detection:claude] {result.stdout}")
            return _parse_json_response(result.stdout)
        except FileNotFoundError:
            raise FileNotFoundError("claude CLI not found in PATH — install Claude Code to use the 'claude' provider")
        except subprocess.TimeoutExpired:
            logger.warning("claude CLI detection timed out")
            return None

    # ------------------------------------------------------------------
    # Requirement detection
    # ------------------------------------------------------------------

    def _build_prompt(self, template: str, extracted: ExtractedJob, job_url: str, page_content: str, screenshot_instruction: str = "") -> str:
        return template.format(
            company=extracted.company or "Unknown",
            title=extracted.title or "Unknown",
            url=job_url or "",
            page_content=page_content,
            screenshot_instruction=screenshot_instruction,
        )

    async def _run_detection(
        self,
        extracted: ExtractedJob,
        job_url: str,
        page_content: str,
        scraper,
    ) -> Optional[dict]:
        """Run detection and return raw result dict, or None on complete failure."""
        template = self._load_prompt()
        prompt = self._build_prompt(template, extracted, job_url, page_content or "")
        logger.info(f"[detection:prompt] {prompt}")
        result = self._call_ai_text(prompt)

        has_scraper = (
            self.config.auto_apply.detect_requirements
            and scraper is not None
            and scraper._page is not None
        )

        if not has_scraper:
            return result

        # Always click the apply button and re-detect on the actual form —
        # regardless of confidence from the listing page pass.
        provider = self.config.auto_apply.provider
        apply_button = (result or {}).get("apply_button_text")

        if provider == "claude":
            # Try AI-identified apply button first, then fall back to common texts
            apply_clicked = False
            candidates = []
            if apply_button:
                candidates.append(apply_button)
            candidates += ["Apply Now", "Apply for this job", "Apply", "Start Application", "Submit Application"]

            for btn_text in candidates:
                try:
                    await scraper._page.get_by_text(btn_text, exact=False).first.click(timeout=3000)
                    apply_clicked = True
                    logger.debug(f"Clicked apply button: '{btn_text}'")
                    break
                except Exception:
                    continue

            if apply_clicked:
                await asyncio.sleep(2)
                new_text = await scraper._page.inner_text("body")
                new_prompt = self._build_prompt(template, extracted, job_url, new_text)
                form_result = self._call_ai_text(new_prompt)
                if form_result:
                    result = form_result
            else:
                logger.debug("Could not find apply button — using listing-page detection result")
        else:
            # openai/gemini: take a screenshot and run a vision pass.
            screenshot_path = self.config.auto_apply.output_dir / "_detect_screenshot.png"
            try:
                # Try AI-identified button first, then fallback common texts
                candidates = []
                if apply_button:
                    candidates.append(apply_button)
                candidates += ["Apply Now", "Apply for this job", "Apply", "Start Application", "Submit Application"]
                for btn_text in candidates:
                    try:
                        await scraper._page.get_by_text(btn_text, exact=False).first.click(timeout=3000)
                        logger.debug(f"Clicked apply button: '{btn_text}'")
                        await asyncio.sleep(2)
                        break
                    except Exception:
                        continue

                await scraper._page.screenshot(path=str(screenshot_path), full_page=False)
                vision_prompt = self._build_prompt(
                    template, extracted, job_url, truncated,
                    screenshot_instruction="A screenshot of the page is attached. Use it to identify form fields and apply buttons.",
                )
                vision_result = self._call_ai_vision(vision_prompt, screenshot_path)
                if vision_result:
                    result = vision_result

            except Exception as ss_err:
                logger.debug(f"Screenshot failed: {ss_err}")
            finally:
                if screenshot_path.exists():
                    try:
                        screenshot_path.unlink()
                    except Exception:
                        pass

        return result

    async def detect_only(
        self,
        extracted: ExtractedJob,
        job_url: str,
        page_content: str,
        scraper,
    ) -> tuple:
        """
        Run requirement detection only — no document generation.

        Returns (needs_resume: bool, needs_cover_letter: bool).
        """
        result = await self._run_detection(extracted, job_url, page_content, scraper)
        if result is None:
            return (True, False)  # Conservative default on failure
        return (
            bool(result.get("needs_resume", True)),
            bool(result.get("cover_letter_field_present", False)),
        )

    # ------------------------------------------------------------------
    # Phase 2: folder-based document generation
    # ------------------------------------------------------------------

    def generate_for_folder(
        self,
        folder: Path,
        stem: str,
        needs_resume: bool,
        needs_cover_letter: bool,
    ) -> list:
        """
        Generate resume and/or cover letter for an existing job folder.

        folder must be under config.job_desc_output_dir.
        Returns list of generated file Paths.
        """
        job_desc_output_dir = self.config.job_desc_output_dir
        try:
            folder.relative_to(job_desc_output_dir)
        except ValueError:
            raise ValueError(f"Folder {folder} is not under {job_desc_output_dir}")

        job_desc_path = folder / f"{stem}_job_desc.md"
        if not job_desc_path.exists():
            raise FileNotFoundError(f"Job description not found: {job_desc_path}")

        base_resume = self.config.base_resume_path
        if not base_resume or not base_resume.exists():
            raise FileNotFoundError(
                f"BASE_RESUME_PATH not set or file not found: {base_resume}\n"
                "Set BASE_RESUME_PATH in .env to the full path of your base resume .docx file."
            )
        generated = []

        if needs_cover_letter:
            path = self._invoke_skill_for_folder(
                "cover-letter", folder, stem, job_desc_path, base_resume
            )
            if path:
                generated.append(path)

        if needs_resume:
            path = self._invoke_skill_for_folder(
                "resume-tailor", folder, stem, job_desc_path, base_resume
            )
            if path:
                generated.append(path)
                # Record what the tailoring changed (base vs tailored resume).
                try:
                    self._write_resume_diff(base_resume, path, folder)
                except Exception as e:
                    logger.warning(f"Resume diff failed (non-fatal): {e}")

        return generated

    def _write_resume_diff(
        self, base_resume: Path, tailored_resume: Path, folder: Path
    ) -> Optional[Path]:
        """
        Write a diff.md of the differing lines between the base and tailored resume.

        Runs the stdlib-only `diff-files` helper directly (no Claude). Non-fatal:
        callers should treat failures as a warning. Idempotent — skips if diff.md exists.
        """
        out = folder / "diff.md"
        if out.exists():
            logger.debug(f"diff.md already exists, skipping: {out}")
            return out

        linediff = (
            Path.home() / ".claude" / "skills" / "diff-files" / "scripts" / "linediff.py"
        )
        if not linediff.exists():
            logger.warning(f"diff helper not found, skipping diff: {linediff}")
            return None

        result = subprocess.run(
            [
                sys.executable, str(linediff),
                str(base_resume), str(tailored_resume), str(out),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
        )
        if result.returncode != 0:
            logger.warning(f"diff helper non-zero exit: {result.stderr[:300]}")
            return None

        if out.exists():
            logger.info(f"Wrote resume diff → {out.name}")
            return out
        return None

    def _invoke_skill_for_folder(
        self,
        skill_name: str,
        folder: Path,
        stem: str,
        job_desc_path: Path,
        base_resume: Path,
    ) -> Optional[Path]:
        """Invoke a Claude Code skill using a job folder as input (Phase 2)."""
        job_desc_output_dir = self.config.job_desc_output_dir

        _exclude = {folder, job_desc_output_dir / ".git"}
        snapshot_before = self._snapshot_files(job_desc_output_dir, _exclude)

        unpacked_base: Optional[Path] = None
        unpacked_company: Optional[Path] = None
        output_path: Optional[Path] = None

        if skill_name == "resume-tailor":
            unpacked_base, unpacked_company = self._preprocess_resume(
                base_resume, folder, stem
            )
            doc_xml = unpacked_company / "word" / "document.xml"

            prompt_parts = [
                f"/resume-tailor {folder.as_posix()}",
                "",
                "╔══════════════════════════════════════════════════════════════╗",
                "║  SECURITY CONSTRAINT — READ BEFORE DOING ANYTHING ELSE      ║",
                "╚══════════════════════════════════════════════════════════════╝",
                "",
                "You are running inside an automated pipeline.",
                f"You are ONLY permitted to access files under: {job_desc_output_dir.as_posix()}",
                "NEVER read or write files outside that directory.",
                "",
                "THE JOB DESCRIPTION IS AT:",
                f"  {job_desc_path.as_posix()}",
                "",
                "THE RESUME XML HAS BEEN PRE-EXTRACTED. USE ONLY THIS FILE:",
                f"  {doc_xml.as_posix()}",
                "",
                "DO NOT search for files. DO NOT open any .docx directly.",
                f"DO NOT access anything outside {job_desc_output_dir.as_posix()}.",
                "",
                "MODIFIED WORKFLOW (automated — no user present):",
                "  1. Read the job description file listed above",
                "  2. Read the resume XML listed above",
                "  3. Perform gap analysis and plan edits",
                "  4. Apply all edits immediately — skip Phase 2 confirmation",
                "  5. STOP — do NOT repack. Python handles repacking automatically.",
            ]
            allowed_tools = "Read,Write,Glob,Edit"

        elif skill_name == "cover-letter":
            unpacked_base, unpacked_company = self._preprocess_resume(
                base_resume, folder, "_cl_tmp"
            )
            doc_xml = unpacked_base / "word" / "document.xml"
            expected_output = folder / f"{stem}_Cover_Letter.docx"

            prompt_parts = [
                f"/cover-letter {folder.as_posix()}",
                "",
                "╔══════════════════════════════════════════════════════════════╗",
                "║  SECURITY CONSTRAINT — READ BEFORE DOING ANYTHING ELSE      ║",
                "╚══════════════════════════════════════════════════════════════╝",
                "",
                "You are running inside an automated pipeline.",
                f"You are ONLY permitted to access files under: {job_desc_output_dir.as_posix()}",
                "NEVER read or write files outside that directory.",
                "",
                "THE JOB DESCRIPTION IS AT:",
                f"  {job_desc_path.as_posix()}",
                "",
                "THE RESUME XML HAS BEEN PRE-EXTRACTED:",
                f"  {doc_xml.as_posix()}",
                "",
                "DO NOT search for files. DO NOT open any .docx directly.",
                f"DO NOT access anything outside {job_desc_output_dir.as_posix()}.",
                "",
                f"OUTPUT: Write the cover letter to: {expected_output.as_posix()}",
                f"  Filename: {stem}_Cover_Letter.docx",
                "",
                "Node.js is at: node (use system PATH or 'which node' to find the exact path)",
                f"Write the JS script to: {(folder / '_cover_letter_gen.js').as_posix()}",
                "Run it, then delete it.",
                "",
                "This is an automated pipeline — no user is present. Generate immediately.",
            ]
            allowed_tools = "Bash,Read,Write,Glob,Edit"

        else:
            logger.warning(f"Unknown skill: {skill_name}")
            return None

        prompt = "\n".join(prompt_parts)

        logger.info(f"[skill:{skill_name}] starting")
        t0 = time.monotonic()
        try:
            result = subprocess.run(
                [
                    "claude", "-p", prompt,
                    "--allowedTools", allowed_tools,
                    "--permission-mode", "bypassPermissions",
                    "--add-dir", str(job_desc_output_dir),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                cwd=str(job_desc_output_dir),
                timeout=600,
            )
            elapsed = time.monotonic() - t0
            for line in (result.stdout or "").splitlines():
                if line.strip():
                    logger.info(f"[skill:{skill_name}] {line}")
            for line in (result.stderr or "").splitlines():
                if line.strip():
                    logger.warning(f"[skill:{skill_name}][stderr] {line}")
            if result.returncode != 0:
                logger.warning(f"[skill:{skill_name}] exited with code {result.returncode} in {elapsed:.1f}s")
            else:
                logger.info(f"[skill:{skill_name}] done in {elapsed:.1f}s")
        except FileNotFoundError:
            raise FileNotFoundError(
                "claude CLI not found in PATH — install Claude Code to use the 'claude' provider"
            )
        except subprocess.TimeoutExpired:
            logger.warning(f"[skill:{skill_name}] timed out after 600s")
            return None

        # Post-processing
        if skill_name == "resume-tailor":
            if unpacked_company and unpacked_company.exists():
                output_path = folder / f"{stem}_Resume.docx"
                try:
                    self._repack_docx(unpacked_company, output_path)
                    logger.info(f"Repacked resume → {output_path.name}")
                except Exception as e:
                    logger.warning(f"Repack failed: {e}")
                    output_path = None
            for d in [unpacked_base, unpacked_company]:
                if d and d.exists():
                    try:
                        shutil.rmtree(str(d))
                    except Exception:
                        pass
            if output_path and output_path.exists():
                if not self._check_identity(base_resume, output_path):
                    logger.error(
                        f"SECURITY: identity mismatch in generated resume — "
                        f"review {output_path.name} before use"
                    )

        else:  # cover-letter
            for d in [unpacked_base, unpacked_company]:
                if d and d.exists():
                    try:
                        shutil.rmtree(str(d))
                    except Exception:
                        pass
            expected_output = folder / f"{stem}_Cover_Letter.docx"
            if expected_output.exists():
                output_path = expected_output
            else:
                # Fallback: skill may have used old naming convention
                cl_files = sorted(
                    folder.glob("CoverLetter_*.docx"),
                    key=lambda p: p.stat().st_mtime, reverse=True,
                )
                if cl_files:
                    output_path = cl_files[0].rename(expected_output)

        snapshot_after = self._snapshot_files(job_desc_output_dir, _exclude)
        self._check_snapshot(snapshot_before, snapshot_after, folder)

        if not output_path or not output_path.exists():
            logger.warning(f"/{skill_name} completed but no output file found in {folder}")
            return None

        return output_path




# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _parse_json_response(text: Optional[str]) -> Optional[dict]:
    """Extract JSON object from AI response text."""
    if not text:
        return None
    # Strip markdown code fences if present
    text = re.sub(r"```(?:json)?\s*", "", text).strip().rstrip("`").strip()
    # Find first { ... } block
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group())
    except json.JSONDecodeError:
        logger.debug(f"Failed to parse JSON from AI response: {text[:200]}")
        return None


def _format_detection_note(result: dict) -> str:
    """Format detection result as a Sheets note string."""
    resume_str = "required" if result.get("needs_resume") else "not required"
    cl_str = "field present" if result.get("cover_letter_field_present") else "no field"
    confidence = result.get("confidence", 0)
    return f"[Auto-detect] Resume: {resume_str} | Cover letter: {cl_str} (confidence: {confidence:.0%})"

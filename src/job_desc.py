"""
Job description saving for ApplyPotato Phase 1.

Saves the raw scraped page text and a Claude-formatted markdown summary
into a per-job folder under JOB_DESC_OUTPUT_DIR.
"""

import logging
import re
import subprocess
from pathlib import Path
from typing import Optional

from .ai_extractor import ExtractedJob

logger = logging.getLogger(__name__)


def _sanitize(s: str, max_len: int = 50) -> str:
    s = re.sub(r'[^\w\s-]', '', s).strip()
    s = re.sub(r'[\s]+', '_', s)
    return s[:max_len]


def _build_format_prompt(raw_content: str) -> str:
    """Build the prompt for the format-job-desc skill."""
    return f"/format-job-desc\n\n{raw_content}"


def save_job_description(
    row_num: int,
    company: str,
    page_content: str,
    extracted: ExtractedJob,
    base_dir: Path,
    project_root: Optional[Path] = None,
) -> Optional[Path]:
    """
    Create a per-job folder under base_dir and write raw + formatted job description files.

    Returns the path to the formatted _job_desc.md file, or None on failure.
    """
    stem = f"{row_num}_{_sanitize(company)}"
    folder = base_dir / stem
    folder.mkdir(parents=True, exist_ok=True)

    # Save raw scraped text
    raw_path = folder / f"{stem}_raw.txt"
    try:
        raw_path.write_text(page_content, encoding="utf-8")
    except Exception as e:
        logger.warning(f"Failed to save raw job content: {e}")

    # Format with Claude via the format-job-desc skill
    md_path = folder / f"{stem}_job_desc.md"
    prompt = _build_format_prompt(page_content)

    cwd = str(project_root) if project_root else str(Path(__file__).parent.parent)
    try:
        result = subprocess.run(
            ["claude", "-p", prompt, "--allowedTools", ""],
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=cwd,
            timeout=120,
        )
        if result.returncode == 0 and result.stdout.strip():
            md_path.write_text(result.stdout.strip(), encoding="utf-8")
            logger.info(f"  Job description saved: {md_path}")
            return md_path
        else:
            logger.warning(f"  Claude formatting failed (rc={result.returncode}): {result.stderr[:200]}")
    except FileNotFoundError:
        logger.warning("  claude CLI not found — writing raw content as markdown fallback")
    except subprocess.TimeoutExpired:
        logger.warning("  claude CLI timed out for job description formatting")
    except Exception as e:
        logger.warning(f"  Job description formatting error: {e}")

    # Fallback: write a basic markdown version without Claude
    try:
        fallback = _fallback_markdown(extracted, page_content)
        md_path.write_text(fallback, encoding="utf-8")
        logger.info(f"  Job description saved (fallback): {md_path}")
        return md_path
    except Exception as e:
        logger.warning(f"  Fallback markdown write failed: {e}")

    return None


def _fallback_markdown(extracted: ExtractedJob, raw_content: str) -> str:
    """Minimal markdown formatter used when Claude CLI is unavailable."""
    lines = [
        f"# {extracted.title or 'Unknown Role'} at {extracted.company or 'Unknown Company'}",
        "",
        "## Details",
        f"- **Type:** {extracted.job_type or 'N/A'}",
        f"- **Work Model:** {extracted.work_model or 'N/A'}",
        f"- **Season/Year:** {extracted.season_year or 'N/A'}",
        f"- **Deadline:** {extracted.deadline or 'N/A'}",
        f"- **Work Auth:** {extracted.work_authorization or 'N/A'}",
        "",
    ]
    if extracted.required_skills:
        lines += ["## Required Skills", ""] + [f"- {s}" for s in extracted.required_skills] + [""]
    if extracted.preferred_skills:
        lines += ["## Preferred Skills", ""] + [f"- {s}" for s in extracted.preferred_skills] + [""]
    lines += ["## Raw Content", "", "```", raw_content[:3000], "```"]
    return "\n".join(lines)


def commit_and_push_job_folder(folder: Path, repo_dir: Path, stem: str) -> bool:
    """
    Git add + commit + push the new job folder inside repo_dir.

    Returns True if push succeeded.
    """
    try:
        # Check it's a git repo
        check = subprocess.run(
            ["git", "-C", str(repo_dir), "rev-parse", "--git-dir"],
            capture_output=True, text=True,
        )
        if check.returncode != 0:
            logger.warning(f"  {repo_dir} is not a git repository — skipping push")
            return False

        rel_folder = folder.relative_to(repo_dir).as_posix()

        subprocess.run(
            ["git", "-C", str(repo_dir), "add", rel_folder],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(repo_dir), "commit", "-m", f"Add job: {stem}"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(repo_dir), "push"],
            check=True, capture_output=True,
        )
        logger.info(f"  Pushed {stem} to Resume repo")
        return True
    except subprocess.CalledProcessError as e:
        stderr = e.stderr.decode() if isinstance(e.stderr, bytes) else (e.stderr or "")
        if "nothing to commit" in stderr:
            logger.debug(f"  Nothing new to push for {stem}")
            return True
        logger.warning(f"  Git push failed for {stem}: {stderr[:200]}")
        return False
    except Exception as e:
        logger.warning(f"  Git push error: {e}")
        return False

import logging
import subprocess
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def run_project_fit_skill(
    job_url: str,
    project_name: str,
    project_root: Path,
    page_content: Optional[str] = None,
) -> None:
    """Invoke the project-fit Claude Code skill for a dream job.

    Pass page_content if the page was already scraped (e.g. via Playwright) to
    avoid a redundant fetch. If omitted, the skill will fetch the URL itself.
    """
    resume_dir = Path.home() / "Documents" / "Resume"
    if page_content:
        prompt = (
            f"/project-fit for {project_name}\n\n"
            f"Job URL: {job_url}\n\n"
            f"<page_content>\n{page_content}\n</page_content>"
        )
    else:
        prompt = f"/project-fit {job_url} for {project_name}"
    allowed_tools = "Read,Write,Glob" if page_content else "WebFetch,Read,Write,Glob"
    try:
        result = subprocess.run(
            [
                "claude", "-p", prompt,
                "--allowedTools", allowed_tools,
                "--permission-mode", "bypassPermissions",
                "--add-dir", str(resume_dir),
            ],
            capture_output=True, text=True, encoding="utf-8",
            cwd=str(project_root), timeout=180,
        )
        for line in (result.stdout or "").splitlines():
            if line.strip():
                logger.info(f"[project-fit] {line}")
        if result.returncode != 0:
            logger.warning(f"[project-fit] exited {result.returncode}")
        else:
            logger.info(f"[project-fit] report updated for {project_name}")
    except FileNotFoundError:
        logger.warning("[project-fit] claude CLI not found — skipping fit report update")
    except subprocess.TimeoutExpired:
        logger.warning("[project-fit] timed out after 180s")


if __name__ == "__main__":
    import asyncio
    import sys
    import logging as _logging
    from src.config import get_config
    from src.scraper import PlaywrightScraper

    _logging.basicConfig(level=_logging.INFO, format="%(message)s")

    if len(sys.argv) < 2:
        print("Usage: python -m src.project_fit <url> [project_name]")
        sys.exit(1)

    _url = sys.argv[1]
    _project_name = sys.argv[2] if len(sys.argv) > 2 else Path.cwd().name

    async def _run() -> None:
        config = get_config()
        async with PlaywrightScraper(config) as scraper:
            content, final_url, _ = await scraper.fetch_page(_url)
        run_project_fit_skill(final_url or _url, _project_name, Path.cwd(), page_content=content)

    asyncio.run(_run())

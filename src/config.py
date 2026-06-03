"""
Configuration loader for ApplyPotato.
Loads and validates environment variables from .env file.
"""

import os
import sys
from pathlib import Path
from typing import Dict, List, Optional
from dataclasses import dataclass, field
from dotenv import load_dotenv


@dataclass
class GitHubRepo:
    """GitHub repository configuration."""
    owner_repo: str  # e.g., "owner/repo"
    branch: str      # e.g., "dev" or "main"


@dataclass
class NewsletterSource:
    """Newsletter source configuration."""
    name: str              # e.g., "simplify"
    sender_emails: List[str]  # e.g., ["noreply@simplify.jobs"]


@dataclass
class DiscordConfig:
    """Discord notification configuration."""
    enabled: bool
    webhook_url: str              # dream-company job alerts
    form_fill_webhook_url: str    # fill-form auto-run summaries (separate channel)
    dream_company_min_salary_annual: Optional[float]  # None = no threshold
    dream_company_min_salary_hourly: Optional[float]  # None = no threshold


@dataclass
class AutoApplyConfig:
    """Auto-apply document generation configuration."""
    enabled: bool
    resume_path: Optional[Path]
    background_path: Optional[Path]
    output_dir: Path
    provider: str          # "claude" | "openai" | "gemini"
    detect_requirements: bool


@dataclass
class UserProfile:
    """User profile configuration for job filtering and scoring."""
    name: str
    email: str
    class_standing: Optional[str]  # None if graduated
    graduation_date: str
    majors: List[str]
    minors: List[str]
    gpa: float
    work_authorization: str
    target_job_type: str  # Internship, Full-Time, Both
    target_season_year: Optional[str]  # None for ASAP/any
    preferred_locations: List[str]
    work_model: str  # Remote, Hybrid, On-site, Any
    min_salary_hourly: float
    target_companies: List[str]
    skills: List[str]
    job_categories: List[str]  # Software Engineering, Product Management, Data Science/AI/ML, etc.
    degree_level: str  # Bachelors, Masters, PhD, MBA


@dataclass
class Config:
    """Main configuration class for ApplyPotato."""

    # API Keys
    openai_api_key: Optional[str]
    gemini_api_key: Optional[str]
    google_credentials_path: Path
    google_sheet_id: str

    # AI Settings
    ai_provider: str  # "openai" or "gemini"
    openai_model: str
    gemini_model: str
    openai_max_tokens: Optional[int]  # None = use model default (16384 for gpt-4o-mini)
    gemini_max_output_tokens: Optional[int]  # None = use model default (8192 for gemini-2.0-flash)

    # Job Sources
    github_repos: List[GitHubRepo]
    job_age_limit_days: int  # Only process jobs posted within this many days

    # Newsletter Sources
    newsletter_sources: List[NewsletterSource]
    newsletter_lookback_days: int
    newsletter_enabled: bool

    # Schedule Settings
    scrape_interval_minutes: int
    gmail_check_interval_minutes: int
    gmail_lookback_days: int

    # User Profile
    user: UserProfile

    # Status Colors (hex codes for Google Sheets row highlighting)
    status_colors: Dict[str, str]  # e.g., {"Applied": "#E3F2FD", "OA": "#B3E5FC"}

    # Discord Notifications
    discord: DiscordConfig

    # Auto-Apply Document Generation
    auto_apply: AutoApplyConfig

    # Job Description Output (Phase 1 — saved per job, committed to Resume repo)
    job_desc_output_dir: Path

    # Base resume for document generation (Phase 2)
    base_resume_path: Path

    # Advanced Settings
    max_retries: int
    page_timeout_seconds: int
    render_delay_seconds: float  # JS render delay after page load
    retry_base_delay_seconds: float  # Base delay for exponential backoff
    seen_sources_ttl_days: int  # How long to remember processed source URLs
    log_level: str
    oauth_local_port: int
    oauth_timeout_seconds: int

    # Paths
    base_dir: Path = field(default_factory=lambda: Path(__file__).parent.parent)

    @property
    def auth_dir(self) -> Path:
        return self.base_dir / "auth"

    @property
    def data_dir(self) -> Path:
        return self.base_dir / "data"

    @property
    def logs_dir(self) -> Path:
        return self.base_dir / "logs"

    @property
    def prompts_dir(self) -> Path:
        return self.base_dir / "prompts"


def _parse_list(value: str) -> List[str]:
    """Parse comma-separated string into list, stripping whitespace."""
    if not value or value.strip().lower() == "any":
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _parse_github_repos(value: str) -> List[GitHubRepo]:
    """
    Parse GITHUB_REPOS with format: owner/repo@branch,owner/repo@branch

    If no branch is specified, defaults to "main".
    """
    repos = []
    for item in value.split(","):
        item = item.strip()
        if not item:
            continue
        if "@" in item:
            owner_repo, branch = item.rsplit("@", 1)
        else:
            owner_repo, branch = item, "main"  # Default to main
        repos.append(GitHubRepo(owner_repo=owner_repo.strip(), branch=branch.strip()))
    return repos


def _parse_newsletter_sources(value: str) -> List[NewsletterSource]:
    """
    Parse NEWSLETTER_SOURCES with format: name|email1;email2,name2|email3

    Example: simplify|noreply@simplify.jobs,levels-fyi|alerts@levels.fyi
    """
    sources = []
    if not value or not value.strip():
        return sources

    for item in value.split(","):
        item = item.strip()
        if not item:
            continue
        if "|" not in item:
            continue
        name, emails_str = item.split("|", 1)
        emails = [e.strip() for e in emails_str.split(";") if e.strip()]
        if name.strip() and emails:
            sources.append(NewsletterSource(name=name.strip(), sender_emails=emails))
    return sources


def _get_required(key: str) -> str:
    """Get required environment variable or exit with error."""
    value = os.getenv(key)
    if not value:
        print(f"ERROR: Required environment variable {key} is not set.")
        print(f"Please check your .env file and ensure {key} has a value.")
        sys.exit(1)
    return value


def _get_optional(key: str, default: str = "") -> str:
    """Get optional environment variable with default."""
    return os.getenv(key, default)


def _get_float(key: str, default: float) -> float:
    """Get float environment variable with default."""
    value = os.getenv(key)
    if not value:
        return default
    try:
        return float(value)
    except ValueError:
        print(f"WARNING: {key} value '{value}' is not a valid number. Using default: {default}")
        return default


def _get_int(key: str, default: int) -> int:
    """Get integer environment variable with default."""
    value = os.getenv(key)
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        print(f"WARNING: {key} value '{value}' is not a valid integer. Using default: {default}")
        return default


def _get_optional_int(key: str) -> Optional[int]:
    """Get optional integer environment variable. Returns None if not set."""
    value = os.getenv(key)
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        print(f"WARNING: {key} value '{value}' is not a valid integer. Ignoring.")
        return None


def _parse_status_colors() -> Dict[str, str]:
    """
    Parse STATUS_COLOR_* environment variables into a dict.

    Returns:
        Dict mapping status names to hex color codes.
        e.g., {"Applied": "#E3F2FD", "OA": "#B3E5FC"}
    """
    # Status names must match the values used in sheets.py
    status_names = ["Applied", "OA", "Phone", "Technical", "Offer", "Rejected"]
    colors = {}

    for status in status_names:
        env_key = f"STATUS_COLOR_{status.upper()}"
        color = os.getenv(env_key, "").strip()

        # Validate hex color format
        if color:
            if not color.startswith("#"):
                color = f"#{color}"
            # Check if it's a valid 6-digit hex color
            if len(color) == 7 and all(c in "0123456789ABCDEFabcdef" for c in color[1:]):
                colors[status] = color.upper()
            else:
                print(f"WARNING: {env_key} value '{color}' is not a valid hex color. Ignoring.")

    return colors


def load_config(env_path: Optional[Path] = None) -> Config:
    """
    Load configuration from .env file.

    Args:
        env_path: Optional path to .env file. If not provided, looks in project root.

    Returns:
        Config object with all settings loaded.

    Raises:
        SystemExit: If required configuration is missing.
    """
    # Determine base directory
    base_dir = Path(__file__).parent.parent

    # Load .env file
    if env_path is None:
        env_path = base_dir / ".env"

    if not env_path.exists():
        print(f"ERROR: Configuration file not found: {env_path}")
        print("Please copy .env.example to .env and fill in your values.")
        sys.exit(1)

    load_dotenv(env_path)

    # Validate AI provider configuration
    ai_provider = _get_optional("AI_PROVIDER", "openai").lower()
    if ai_provider not in ("openai", "gemini"):
        print(f"ERROR: AI_PROVIDER must be 'openai' or 'gemini', got '{ai_provider}'")
        sys.exit(1)

    # Get API keys (only the selected provider is required)
    openai_api_key = _get_optional("OPENAI_API_KEY")
    gemini_api_key = _get_optional("GEMINI_API_KEY")

    if ai_provider == "openai" and not openai_api_key:
        print("ERROR: OPENAI_API_KEY is required when AI_PROVIDER is 'openai'")
        sys.exit(1)

    if ai_provider == "gemini" and not gemini_api_key:
        print("ERROR: GEMINI_API_KEY is required when AI_PROVIDER is 'gemini'")
        sys.exit(1)

    # Google credentials path (default: auth/credentials.json)
    creds_path = Path(_get_optional("GOOGLE_CREDENTIALS_PATH", "./auth/credentials.json"))
    if not creds_path.is_absolute():
        creds_path = base_dir / creds_path

    # Parse user profile
    class_standing = _get_optional("USER_CLASS_STANDING")
    user = UserProfile(
        name=_get_optional("USER_NAME", "User"),
        email=_get_required("USER_EMAIL"),
        class_standing=class_standing if class_standing else None,
        graduation_date=_get_optional("USER_GRADUATION_DATE", ""),
        majors=_parse_list(_get_optional("USER_MAJOR", "")),
        minors=_parse_list(_get_optional("USER_MINOR", "")),
        gpa=_get_float("USER_GPA", 0.0),
        work_authorization=_get_optional("USER_WORK_AUTHORIZATION", "Need Sponsorship"),
        target_job_type=_get_optional("USER_TARGET_JOB_TYPE", "Both"),
        target_season_year=_get_optional("USER_TARGET_SEASON_YEAR") or None,
        preferred_locations=_parse_list(_get_optional("USER_PREFERRED_LOCATIONS", "Any")),
        work_model=_get_optional("USER_WORK_MODEL", "Any"),
        min_salary_hourly=_get_float("USER_MIN_SALARY_HOURLY", 0.0),
        target_companies=_parse_list(_get_optional("USER_TARGET_COMPANIES", "")),
        skills=_parse_list(_get_optional("USER_SKILLS", "")),
        job_categories=_parse_list(_get_optional("USER_JOB_CATEGORIES", "")),
        degree_level=_get_optional("USER_DEGREE_LEVEL", "Bachelors"),
    )

    # Parse Discord notification configuration
    discord_enabled = _get_optional("DISCORD_ENABLED", "false").lower() in ("true", "1", "yes")
    discord_webhook = _get_optional("DISCORD_WEBHOOK_URL", "")

    # Salary thresholds: None if unset or "0"
    def _parse_salary_threshold(key: str, default: float) -> Optional[float]:
        raw = _get_optional(key, str(default))
        if not raw or raw.strip() in ("0", ""):
            return None
        try:
            val = float(raw)
            return val if val > 0 else None
        except ValueError:
            return None

    discord_config = DiscordConfig(
        enabled=discord_enabled and bool(discord_webhook),
        webhook_url=discord_webhook,
        form_fill_webhook_url=_get_optional("FORM_FILL_DISCORD_WEBHOOK", ""),
        dream_company_min_salary_annual=_parse_salary_threshold("DREAM_COMPANY_MIN_SALARY_ANNUAL", 100000),
        dream_company_min_salary_hourly=_parse_salary_threshold("DREAM_COMPANY_MIN_SALARY_HOURLY", 50),
    )

    # Parse auto-apply configuration
    auto_apply_enabled = _get_optional("AUTO_APPLY_ENABLED", "false").lower() in ("true", "1", "yes")

    def _resolve_path(key: str) -> Optional[Path]:
        raw = _get_optional(key, "")
        if not raw:
            return None
        p = Path(raw)
        return p if p.is_absolute() else base_dir / p

    resume_path = _resolve_path("AUTO_APPLY_RESUME_PATH")
    background_path = _resolve_path("AUTO_APPLY_BACKGROUND_PATH")

    raw_output_dir = _get_optional("AUTO_APPLY_OUTPUT_DIR", "./output")
    output_dir = Path(raw_output_dir)
    if not output_dir.is_absolute():
        output_dir = base_dir / output_dir

    auto_apply_provider = _get_optional("AUTO_APPLY_PROVIDER", "claude").lower()
    if auto_apply_provider not in ("claude", "openai", "gemini"):
        print(f"WARNING: AUTO_APPLY_PROVIDER must be 'claude', 'openai', or 'gemini', got '{auto_apply_provider}'. Defaulting to 'claude'.")
        auto_apply_provider = "claude"

    auto_apply_config = AutoApplyConfig(
        enabled=auto_apply_enabled,
        resume_path=resume_path,
        background_path=background_path,
        output_dir=output_dir,
        provider=auto_apply_provider,
        detect_requirements=_get_optional("AUTO_APPLY_DETECT_REQUIREMENTS", "true").lower() in ("true", "1", "yes"),
    )

    # Job description output directory (Phase 1 — resume git repo)
    raw_job_desc_dir = _get_optional("JOB_DESC_OUTPUT_DIR", "/Path/To/Your/Resume")
    job_desc_output_dir = Path(raw_job_desc_dir)

    # Base resume path (Phase 2 — optional at load time; validated at use time by generate_docs)
    raw_base_resume = _get_optional("BASE_RESUME_PATH", "")
    base_resume_path = Path(raw_base_resume) if raw_base_resume else Path("")

    # Build config object
    config = Config(
        openai_api_key=openai_api_key if openai_api_key else None,
        gemini_api_key=gemini_api_key if gemini_api_key else None,
        google_credentials_path=creds_path,
        google_sheet_id=_get_required("GOOGLE_SHEET_ID"),
        ai_provider=ai_provider,
        openai_model=_get_optional("OPENAI_MODEL", "gpt-4o-mini"),
        gemini_model=_get_optional("GEMINI_MODEL", "gemini-2.0-flash"),
        openai_max_tokens=_get_optional_int("OPENAI_MAX_TOKENS"),
        gemini_max_output_tokens=_get_optional_int("GEMINI_MAX_OUTPUT_TOKENS"),
        github_repos=_parse_github_repos(_get_required("GITHUB_REPOS")),
        job_age_limit_days=min(_get_int("JOB_AGE_LIMIT_DAYS", 7), 30),  # Cap at 30 days max
        newsletter_sources=_parse_newsletter_sources(_get_optional("NEWSLETTER_SOURCES", "")),
        newsletter_lookback_days=_get_int("NEWSLETTER_LOOKBACK_DAYS", 7),
        newsletter_enabled=_get_optional("NEWSLETTER_ENABLED", "true").lower() in ("true", "1", "yes"),
        scrape_interval_minutes=_get_int("SCRAPE_INTERVAL_MINUTES", 30),
        gmail_check_interval_minutes=_get_int("GMAIL_CHECK_INTERVAL_MINUTES", 10),
        gmail_lookback_days=_get_int("GMAIL_LOOKBACK_DAYS", 1),
        user=user,
        status_colors=_parse_status_colors(),
        discord=discord_config,
        auto_apply=auto_apply_config,
        job_desc_output_dir=job_desc_output_dir,
        base_resume_path=base_resume_path,
        max_retries=_get_int("MAX_RETRIES", 3),
        page_timeout_seconds=_get_int("PAGE_TIMEOUT_SECONDS", 30),
        render_delay_seconds=_get_float("RENDER_DELAY_SECONDS", 1.0),
        retry_base_delay_seconds=_get_float("RETRY_BASE_DELAY_SECONDS", 5.0),
        seen_sources_ttl_days=_get_int("SEEN_SOURCES_TTL_DAYS", 90),
        log_level=_get_optional("LOG_LEVEL", "INFO").upper(),
        oauth_local_port=_get_int("OAUTH_LOCAL_PORT", 8888),
        oauth_timeout_seconds=_get_int("OAUTH_TIMEOUT_SECONDS", 120),
        base_dir=base_dir,
    )

    # Ensure directories exist
    config.auth_dir.mkdir(exist_ok=True)
    config.data_dir.mkdir(exist_ok=True)
    config.logs_dir.mkdir(exist_ok=True)
    config.auto_apply.output_dir.mkdir(parents=True, exist_ok=True)
    config.job_desc_output_dir.mkdir(parents=True, exist_ok=True)

    return config


# Singleton config instance (loaded on first access)
_config: Optional[Config] = None


def get_config() -> Config:
    """Get the global configuration instance."""
    global _config
    if _config is None:
        _config = load_config()
    return _config


def reload_config(env_path: Optional[Path] = None) -> Config:
    """Force reload of configuration."""
    global _config
    _config = load_config(env_path)
    return _config

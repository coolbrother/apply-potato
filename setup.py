#!/usr/bin/env python3
"""
setup.py - First-time setup script for ApplyPotato

Usage:
    python setup.py

This script:
1. Creates virtual environment if missing
2. Checks if running inside venv
3. Installs Python dependencies
4. Installs Playwright browser
5. Creates .env from .env.example if missing
6. Validates configuration
7. Optionally tests OAuth flows
"""

import os
import sys
import shutil
import subprocess
from pathlib import Path

# Project paths
PROJECT_ROOT = Path(__file__).parent.resolve()
VENV_DIR = PROJECT_ROOT / "venv"
REQUIREMENTS_FILE = PROJECT_ROOT / "requirements.txt"
ENV_FILE = PROJECT_ROOT / ".env"
ENV_EXAMPLE_FILE = PROJECT_ROOT / ".env.example"
AUTH_DIR = PROJECT_ROOT / "auth"
CREDENTIALS_FILE = AUTH_DIR / "credentials.json"


def is_windows() -> bool:
    """Check if running on Windows."""
    return sys.platform == "win32"


def is_in_venv() -> bool:
    """Check if currently running inside a virtual environment."""
    return sys.prefix != sys.base_prefix


def get_venv_python() -> Path:
    """Get path to Python executable inside venv."""
    if is_windows():
        return VENV_DIR / "Scripts" / "python.exe"
    else:
        return VENV_DIR / "bin" / "python"


def get_venv_pip() -> Path:
    """Get path to pip inside venv."""
    if is_windows():
        return VENV_DIR / "Scripts" / "pip.exe"
    else:
        return VENV_DIR / "bin" / "pip"


def venv_exists() -> bool:
    """Check if venv directory exists and has a valid Python executable."""
    return get_venv_python().exists()


def print_header(text: str) -> None:
    """Print a section header."""
    print()
    print("=" * 60)
    print(f"  {text}")
    print("=" * 60)


def print_success(text: str) -> None:
    """Print a success message."""
    print(f"[OK] {text}")


def print_warning(text: str) -> None:
    """Print a warning message."""
    print(f"[WARNING] {text}")


def print_error(text: str) -> None:
    """Print an error message."""
    print(f"[ERROR] {text}")


def print_info(text: str) -> None:
    """Print an info message."""
    print(f"[INFO] {text}")


def prompt_yes_no(message: str, default: bool = False) -> bool:
    """Prompt user for yes/no confirmation."""
    suffix = " [Y/n]: " if default else " [y/N]: "
    response = input(message + suffix).strip().lower()
    if not response:
        return default
    return response in ("y", "yes")


def create_venv() -> bool:
    """Create virtual environment."""
    print_info(f"Creating virtual environment at {VENV_DIR}...")
    try:
        subprocess.run(
            [sys.executable, "-m", "venv", str(VENV_DIR)],
            check=True,
            capture_output=True,
            text=True
        )
        return True
    except subprocess.CalledProcessError as e:
        print_error(f"Failed to create venv: {e.stderr}")
        return False


def print_activation_instructions() -> None:
    """Print platform-specific venv activation instructions."""
    print()
    print("To activate the virtual environment:")
    print()
    if is_windows():
        print(f"    {VENV_DIR}\\Scripts\\activate")
    else:
        print(f"    source {VENV_DIR}/bin/activate")
    print()
    print("Then run this script again:")
    print()
    print("    python setup.py")


def install_requirements() -> bool:
    """Install Python dependencies from requirements.txt."""
    if not REQUIREMENTS_FILE.exists():
        print_error(f"requirements.txt not found at {REQUIREMENTS_FILE}")
        return False

    print_info("Installing Python dependencies...")
    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "-r", str(REQUIREMENTS_FILE)],
            check=True
        )
        return True
    except subprocess.CalledProcessError as e:
        print_error(f"Failed to install dependencies: {e}")
        return False


def install_playwright() -> bool:
    """Install Playwright Chromium browser."""
    print_info("Installing Playwright Chromium browser...")
    try:
        subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            check=True
        )
        return True
    except subprocess.CalledProcessError as e:
        print_error(f"Failed to install Playwright: {e}")
        return False


def setup_env_file() -> bool:
    """Copy .env.example to .env if .env doesn't exist."""
    if ENV_FILE.exists():
        print_success(".env file already exists")
        return True

    if not ENV_EXAMPLE_FILE.exists():
        print_error(f".env.example not found at {ENV_EXAMPLE_FILE}")
        return False

    print_info(f"Creating .env from .env.example...")
    try:
        shutil.copy(ENV_EXAMPLE_FILE, ENV_FILE)
        print_success(".env file created")
        print()
        print_warning("You need to edit .env with your settings:")
        print("  - OPENAI_API_KEY or GEMINI_API_KEY")
        print("  - GOOGLE_SHEET_ID")
        print("  - User profile settings (USER_EMAIL, etc.)")
        print()
        print(f"Edit the file at: {ENV_FILE}")
        return True
    except OSError as e:
        print_error(f"Failed to create .env: {e}")
        return False


def check_credentials_file() -> bool:
    """Check if Google credentials.json exists."""
    if CREDENTIALS_FILE.exists():
        return True
    return False


def print_google_cloud_instructions() -> None:
    """Print instructions for setting up Google Cloud credentials."""
    print()
    print_warning("Google Cloud credentials not found!")
    print()
    print("To set up Google Cloud credentials:")
    print()
    print("1. Go to https://console.cloud.google.com/")
    print("2. Create a new project (or select existing)")
    print("3. Enable these APIs:")
    print("   - Google Sheets API")
    print("   - Gmail API")
    print("4. Go to 'APIs & Services' > 'Credentials'")
    print("5. Click 'Create Credentials' > 'OAuth client ID'")
    print("6. Select 'Desktop app' as application type")
    print("7. Download the JSON file")
    print(f"8. Save it as: {CREDENTIALS_FILE}")
    print()


def validate_config() -> bool:
    """Validate that configuration loads correctly."""
    print_info("Validating configuration...")
    try:
        # Add src to path so we can import config
        sys.path.insert(0, str(PROJECT_ROOT / "src"))
        from config import load_config
        config = load_config()
        print_success("Configuration loaded successfully")
        return True
    except Exception as e:
        print_error(f"Configuration validation failed: {e}")
        return False


def test_oauth_sheets() -> bool:
    """Test Google Sheets OAuth flow."""
    print_info("Testing Google Sheets OAuth...")
    try:
        sys.path.insert(0, str(PROJECT_ROOT / "src"))
        from sheets import SheetsClient
        from config import load_config

        config = load_config()
        client = SheetsClient(config)
        # Just creating the client triggers OAuth if needed
        print_success("Google Sheets OAuth successful")
        return True
    except Exception as e:
        print_error(f"Sheets OAuth failed: {e}")
        return False


def test_oauth_gmail() -> bool:
    """Test Gmail OAuth flow."""
    print_info("Testing Gmail OAuth...")
    try:
        sys.path.insert(0, str(PROJECT_ROOT / "src"))
        from gmail import GmailClient
        from config import load_config

        config = load_config()
        client = GmailClient(config)
        # Just creating the client triggers OAuth if needed
        print_success("Gmail OAuth successful")
        return True
    except Exception as e:
        print_error(f"Gmail OAuth failed: {e}")
        return False


def main() -> int:
    """Main setup flow."""
    print_header("ApplyPotato Setup")

    # Step 1: Virtual Environment
    print_header("Step 1: Virtual Environment")
    if not venv_exists():
        print_info("Virtual environment not found")
        if not create_venv():
            return 1
        print_success("Virtual environment created")
    else:
        print_success("Virtual environment already exists")

    # Step 2: Check if running in venv
    print_header("Step 2: Environment Check")
    if not is_in_venv():
        print_warning("Not running inside virtual environment!")
        print_activation_instructions()
        return 2
    print_success("Running inside virtual environment")

    # Step 3: Install dependencies
    print_header("Step 3: Installing Dependencies")
    if not install_requirements():
        return 1
    print_success("Dependencies installed")

    # Step 4: Install Playwright
    print_header("Step 4: Installing Playwright Browser")
    if not install_playwright():
        return 1
    print_success("Playwright Chromium installed")

    # Step 5: Environment file
    print_header("Step 5: Environment Configuration")
    if not setup_env_file():
        return 1

    # Step 6: Google credentials
    print_header("Step 6: Google Cloud Credentials")
    if not check_credentials_file():
        print_google_cloud_instructions()
        print()
        print("After setting up credentials.json, run setup.py again")
        print("to continue with configuration validation.")
        return 3
    print_success("credentials.json found")

    # Step 7: Validate config
    print_header("Step 7: Configuration Validation")
    if not validate_config():
        print()
        print("Please fix the configuration errors in .env and try again.")
        return 1
    print_success("Configuration validated")

    # Step 8: Optional OAuth testing
    print_header("Step 8: OAuth Testing (Optional)")
    print("Testing OAuth will open your browser for Google authentication.")
    print("This creates token files so the scripts can run automatically.")
    print()
    if prompt_yes_no("Would you like to test Google OAuth flows now?"):
        print()
        test_oauth_sheets()
        test_oauth_gmail()
    else:
        print_info("Skipping OAuth testing")
        print("OAuth will be triggered on first run of scrape_jobs.py or check_gmail.py")

    # Success
    print_header("Setup Complete!")
    print()
    print("You can now run:")
    print()
    print("  python scrape_jobs.py            # Run job scraper once")
    print("  python scrape_jobs.py --scheduled # Run continuously")
    print()
    print("  python check_gmail.py            # Run Gmail checker once")
    print("  python check_gmail.py --scheduled # Run continuously")
    print()
    print("  python install_service.py        # Install as Windows services")
    print()

    return 0


if __name__ == "__main__":
    sys.exit(main())

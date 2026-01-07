#!/usr/bin/env python3
"""
install_service.py - Install ApplyPotato as background services

Usage:
    python install_service.py              # Install services
    python install_service.py --uninstall  # Uninstall services
    python install_service.py --status     # Show service status

Windows: Uses WinSW (Windows Service Wrapper)
macOS: Uses Launch Agents
"""

import os
import sys
import argparse
import subprocess
import shutil
import urllib.request
import json
from pathlib import Path
from typing import Optional, Tuple

# Project paths
PROJECT_ROOT = Path(__file__).parent.resolve()
VENV_DIR = PROJECT_ROOT / "venv"
ENV_FILE = PROJECT_ROOT / ".env"
AUTH_DIR = PROJECT_ROOT / "auth"
CREDENTIALS_FILE = AUTH_DIR / "credentials.json"
TOOLS_DIR = PROJECT_ROOT / "tools"
WINSW_DIR = TOOLS_DIR / "winsw"
LOGS_DIR = PROJECT_ROOT / "logs"

# WinSW settings
WINSW_GITHUB_API = "https://api.github.com/repos/winsw/winsw/releases/latest"
WINSW_EXE = WINSW_DIR / "WinSW-x64.exe"

# Service definitions
SERVICES = {
    "scrape": {
        "id": "ApplyPotatoScrape",
        "name": "ApplyPotato Job Scraper",
        "description": "Scrapes job listings from GitHub repositories",
        "script": "scrape_jobs.py",
        "plist_id": "com.applypotato.scrape"
    },
    "gmail": {
        "id": "ApplyPotatoGmail",
        "name": "ApplyPotato Gmail Checker",
        "description": "Monitors Gmail for job application status updates",
        "script": "check_gmail.py",
        "plist_id": "com.applypotato.gmail"
    }
}


def is_windows() -> bool:
    """Check if running on Windows."""
    return sys.platform == "win32"


def is_macos() -> bool:
    """Check if running on macOS."""
    return sys.platform == "darwin"


def is_admin() -> bool:
    """Check if running with admin privileges."""
    if is_windows():
        try:
            import ctypes
            return ctypes.windll.shell32.IsUserAnAdmin() != 0
        except Exception:
            return False
    else:
        return os.geteuid() == 0


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


def check_prerequisites() -> Tuple[bool, str]:
    """Check all prerequisites before installation."""
    errors = []

    # Check venv
    if is_windows():
        python_path = VENV_DIR / "Scripts" / "python.exe"
    else:
        python_path = VENV_DIR / "bin" / "python"

    if not python_path.exists():
        errors.append(
            f"Virtual environment not found at {VENV_DIR}\n"
            "Run 'python setup_wizard.py' first."
        )

    # Check .env
    if not ENV_FILE.exists():
        errors.append(
            f".env file not found at {ENV_FILE}\n"
            "Run 'python setup_wizard.py' first."
        )

    # Check credentials
    if not CREDENTIALS_FILE.exists():
        errors.append(
            f"Google credentials not found at {CREDENTIALS_FILE}\n"
            "Download from Google Cloud Console."
        )

    if errors:
        return False, "\n\n".join(errors)

    return True, ""


# =============================================================================
# Windows (WinSW) Implementation
# =============================================================================

def download_winsw() -> bool:
    """Download WinSW from GitHub releases."""
    print_info("Downloading WinSW from GitHub...")

    try:
        # Create tools directory
        WINSW_DIR.mkdir(parents=True, exist_ok=True)

        # Get latest release info from GitHub API
        print_info("Fetching latest release info...")
        req = urllib.request.Request(
            WINSW_GITHUB_API,
            headers={"User-Agent": "ApplyPotato-Installer"}
        )
        with urllib.request.urlopen(req, timeout=30) as response:
            release_info = json.loads(response.read().decode())

        # Find the x64 exe asset
        download_url = None
        for asset in release_info.get("assets", []):
            name = asset.get("name", "")
            if "x64" in name.lower() and name.endswith(".exe"):
                download_url = asset.get("browser_download_url")
                break

        if not download_url:
            print_error("Could not find WinSW-x64.exe in latest release")
            return False

        # Download the exe
        print_info(f"Downloading from: {download_url}")
        urllib.request.urlretrieve(download_url, str(WINSW_EXE))

        print_success(f"WinSW downloaded to {WINSW_EXE}")
        return True

    except urllib.error.URLError as e:
        print_error(f"Failed to download WinSW: {e}")
        return False
    except json.JSONDecodeError as e:
        print_error(f"Failed to parse GitHub API response: {e}")
        return False
    except Exception as e:
        print_error(f"Unexpected error downloading WinSW: {e}")
        return False


def get_winsw() -> Optional[Path]:
    """Get WinSW executable, downloading if needed."""
    if WINSW_EXE.exists():
        return WINSW_EXE

    print_info("WinSW not found locally")
    if download_winsw():
        return WINSW_EXE
    return None


def create_winsw_xml(service_key: str) -> str:
    """Generate WinSW XML configuration for a service."""
    service = SERVICES[service_key]
    python_path = VENV_DIR / "Scripts" / "python.exe"
    script_path = PROJECT_ROOT / service["script"]

    # Use absolute paths in XML
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<service>
  <id>{service['id']}</id>
  <name>{service['name']}</name>
  <description>{service['description']}</description>
  <executable>{python_path}</executable>
  <arguments>"{script_path}" --scheduled</arguments>
  <workingdirectory>{PROJECT_ROOT}</workingdirectory>
  <log mode="roll-by-size">
    <sizeThreshold>5120</sizeThreshold>
    <keepFiles>3</keepFiles>
  </log>
  <startmode>Automatic</startmode>
  <onfailure action="restart" delay="10 sec"/>
  <resetfailure>1 hour</resetfailure>
</service>
"""
    return xml


def install_windows_service(service_key: str) -> bool:
    """Install a single Windows service using WinSW."""
    service = SERVICES[service_key]
    service_exe = WINSW_DIR / f"{service['id']}.exe"
    service_xml = WINSW_DIR / f"{service['id']}.xml"

    try:
        # Copy WinSW exe with service name
        shutil.copy(WINSW_EXE, service_exe)

        # Create XML config
        xml_content = create_winsw_xml(service_key)
        with open(service_xml, "w", encoding="utf-8") as f:
            f.write(xml_content)

        # Install the service
        result = subprocess.run(
            [str(service_exe), "install"],
            capture_output=True,
            text=True
        )

        if result.returncode != 0:
            # Check if already installed
            if "already exists" in result.stderr.lower() or "already exists" in result.stdout.lower():
                print_warning(f"{service['id']} is already installed, reinstalling...")
                subprocess.run([str(service_exe), "uninstall"], capture_output=True)
                subprocess.run([str(service_exe), "install"], check=True, capture_output=True)
            else:
                print_error(f"Failed to install {service['id']}: {result.stderr or result.stdout}")
                return False

        # Start the service
        result = subprocess.run(
            [str(service_exe), "start"],
            capture_output=True,
            text=True
        )

        if result.returncode != 0:
            print_warning(f"Service installed but failed to start: {result.stderr or result.stdout}")
            print_info("You may need to start it manually from Services")

        print_success(f"Installed: {service['name']}")
        return True

    except Exception as e:
        print_error(f"Failed to install {service['id']}: {e}")
        return False


def uninstall_windows_service(service_key: str) -> bool:
    """Uninstall a single Windows service."""
    service = SERVICES[service_key]
    service_exe = WINSW_DIR / f"{service['id']}.exe"
    service_xml = WINSW_DIR / f"{service['id']}.xml"

    try:
        if service_exe.exists():
            # Stop the service first
            subprocess.run(
                [str(service_exe), "stop"],
                capture_output=True,
                text=True
            )

            # Uninstall
            result = subprocess.run(
                [str(service_exe), "uninstall"],
                capture_output=True,
                text=True
            )

            if result.returncode != 0 and "does not exist" not in result.stderr.lower():
                print_warning(f"Uninstall warning: {result.stderr or result.stdout}")

            # Remove files
            service_exe.unlink(missing_ok=True)
            service_xml.unlink(missing_ok=True)

        print_success(f"Uninstalled: {service['name']}")
        return True

    except Exception as e:
        print_error(f"Failed to uninstall {service['id']}: {e}")
        return False


def get_windows_service_status(service_key: str) -> str:
    """Get Windows service status."""
    service = SERVICES[service_key]
    service_exe = WINSW_DIR / f"{service['id']}.exe"

    if not service_exe.exists():
        return "Not installed"

    try:
        result = subprocess.run(
            [str(service_exe), "status"],
            capture_output=True,
            text=True
        )
        output = result.stdout.strip() or result.stderr.strip()

        if "Started" in output or "Running" in output:
            return "Running"
        elif "Stopped" in output:
            return "Stopped"
        elif "NonExistent" in output:
            return "Not installed"
        else:
            return output or "Unknown"

    except Exception:
        return "Error"


def install_windows_services() -> bool:
    """Install all Windows services."""
    print_header("Installing Windows Services")

    # Check admin rights
    if not is_admin():
        print_error("Administrator privileges required!")
        print()
        print("Please run this command as Administrator:")
        print("  1. Right-click on Command Prompt or PowerShell")
        print("  2. Select 'Run as administrator'")
        print("  3. Navigate to project directory")
        print("  4. Run: python install_service.py")
        return False

    # Get WinSW
    winsw = get_winsw()
    if not winsw:
        print_error("Could not get WinSW executable")
        return False

    # Ensure logs directory exists
    LOGS_DIR.mkdir(exist_ok=True)

    # Install each service
    success = True
    for key in SERVICES:
        if not install_windows_service(key):
            success = False

    if success:
        print()
        print_success("All services installed!")
        print()
        print("Services will start automatically on system boot.")
        print("You can manage them in Windows Services (services.msc)")

    return success


def uninstall_windows_services() -> bool:
    """Uninstall all Windows services."""
    print_header("Uninstalling Windows Services")

    if not is_admin():
        print_error("Administrator privileges required!")
        return False

    success = True
    for key in SERVICES:
        if not uninstall_windows_service(key):
            success = False

    if success:
        print()
        print_success("All services uninstalled!")

    return success


# =============================================================================
# macOS (Launch Agent) Implementation
# =============================================================================

def get_launch_agents_dir() -> Path:
    """Get user's LaunchAgents directory."""
    return Path.home() / "Library" / "LaunchAgents"


def create_plist_content(service_key: str) -> str:
    """Generate plist file content for a macOS Launch Agent."""
    service = SERVICES[service_key]
    python_path = VENV_DIR / "bin" / "python"
    script_path = PROJECT_ROOT / service["script"]
    stdout_log = LOGS_DIR / f"{service_key}_stdout.log"
    stderr_log = LOGS_DIR / f"{service_key}_stderr.log"

    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{service["plist_id"]}</string>

    <key>ProgramArguments</key>
    <array>
        <string>{python_path}</string>
        <string>{script_path}</string>
        <string>--scheduled</string>
    </array>

    <key>WorkingDirectory</key>
    <string>{PROJECT_ROOT}</string>

    <key>RunAtLoad</key>
    <true/>

    <key>KeepAlive</key>
    <true/>

    <key>StandardOutPath</key>
    <string>{stdout_log}</string>

    <key>StandardErrorPath</key>
    <string>{stderr_log}</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin</string>
    </dict>
</dict>
</plist>
"""
    return plist


def install_macos_agent(service_key: str) -> bool:
    """Install a single macOS Launch Agent."""
    service = SERVICES[service_key]
    launch_agents_dir = get_launch_agents_dir()
    plist_path = launch_agents_dir / f"{service['plist_id']}.plist"

    try:
        # Ensure directory exists
        launch_agents_dir.mkdir(parents=True, exist_ok=True)

        # Unload existing agent if present
        if plist_path.exists():
            subprocess.run(
                ["launchctl", "unload", str(plist_path)],
                capture_output=True
            )

        # Write plist file
        plist_content = create_plist_content(service_key)
        with open(plist_path, "w") as f:
            f.write(plist_content)

        # Load the agent
        result = subprocess.run(
            ["launchctl", "load", str(plist_path)],
            capture_output=True,
            text=True
        )

        if result.returncode != 0:
            print_error(f"Failed to load agent: {result.stderr}")
            return False

        print_success(f"Installed: {service['name']}")
        return True

    except Exception as e:
        print_error(f"Failed to install {service['plist_id']}: {e}")
        return False


def uninstall_macos_agent(service_key: str) -> bool:
    """Uninstall a single macOS Launch Agent."""
    service = SERVICES[service_key]
    plist_path = get_launch_agents_dir() / f"{service['plist_id']}.plist"

    try:
        if plist_path.exists():
            # Unload the agent
            subprocess.run(
                ["launchctl", "unload", str(plist_path)],
                capture_output=True
            )

            # Remove plist file
            plist_path.unlink()

        print_success(f"Uninstalled: {service['name']}")
        return True

    except Exception as e:
        print_error(f"Failed to uninstall {service['plist_id']}: {e}")
        return False


def get_macos_agent_status(service_key: str) -> str:
    """Get macOS agent status."""
    service = SERVICES[service_key]
    plist_path = get_launch_agents_dir() / f"{service['plist_id']}.plist"

    if not plist_path.exists():
        return "Not installed"

    try:
        result = subprocess.run(
            ["launchctl", "list", service["plist_id"]],
            capture_output=True,
            text=True
        )

        if result.returncode == 0:
            # Parse the output - format: PID Status Label
            lines = result.stdout.strip().split("\n")
            if lines:
                parts = lines[0].split()
                if len(parts) >= 2:
                    pid = parts[0]
                    if pid == "-":
                        return "Stopped"
                    else:
                        return f"Running (PID: {pid})"
            return "Running"
        else:
            return "Stopped"

    except Exception:
        return "Error"


def install_macos_agents() -> bool:
    """Install all macOS Launch Agents."""
    print_header("Installing macOS Launch Agents")

    # Ensure logs directory exists
    LOGS_DIR.mkdir(exist_ok=True)

    success = True
    for key in SERVICES:
        if not install_macos_agent(key):
            success = False

    if success:
        print()
        print_success("All agents installed!")
        print()
        print("Agents will start automatically on login.")
        print("You can manage them with launchctl commands.")

    return success


def uninstall_macos_agents() -> bool:
    """Uninstall all macOS Launch Agents."""
    print_header("Uninstalling macOS Launch Agents")

    success = True
    for key in SERVICES:
        if not uninstall_macos_agent(key):
            success = False

    if success:
        print()
        print_success("All agents uninstalled!")

    return success


# =============================================================================
# Status and Main
# =============================================================================

def print_service_status() -> None:
    """Print status of all services."""
    print_header("Service Status")
    print()

    for key, service in SERVICES.items():
        if is_windows():
            status = get_windows_service_status(key)
        elif is_macos():
            status = get_macos_agent_status(key)
        else:
            status = "Unsupported OS"

        print(f"  {service['name']}: {status}")

    print()


def main() -> int:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Install ApplyPotato as background services"
    )
    parser.add_argument(
        "--uninstall",
        action="store_true",
        help="Uninstall services"
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Show service status"
    )
    args = parser.parse_args()

    # Status check doesn't need prerequisites
    if args.status:
        print_service_status()
        return 0

    # Check prerequisites for install/uninstall
    ok, error = check_prerequisites()
    if not ok:
        print_header("Prerequisites Check Failed")
        print()
        print(error)
        return 1

    # Route to appropriate OS handler
    if is_windows():
        if args.uninstall:
            return 0 if uninstall_windows_services() else 1
        else:
            return 0 if install_windows_services() else 1

    elif is_macos():
        if args.uninstall:
            return 0 if uninstall_macos_agents() else 1
        else:
            return 0 if install_macos_agents() else 1

    else:
        print_error(f"Unsupported operating system: {sys.platform}")
        print()
        print("Services can only be installed on Windows or macOS.")
        print()
        print("For Linux, you can create a systemd service manually:")
        print("  1. Create a .service file in /etc/systemd/system/")
        print("  2. Enable and start with: systemctl enable --now <service>")
        return 1


if __name__ == "__main__":
    sys.exit(main())

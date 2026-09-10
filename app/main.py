from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "history.sqlite3"

app = FastAPI(title="Linux Package Manager", version="0.5.0")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="pkg-op")
_operations: dict[str, dict[str, Any]] = {}
_operations_lock = threading.Lock()

# Volatile catalog used to enrich details for search results that are available remotely
# but not currently installed. It is intentionally not persisted as package-manager data
# remains the source of truth.
_search_catalog: dict[tuple[str, str], dict[str, Any]] = {}
_search_catalog_lock = threading.Lock()

PACKAGE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9+_.:@/-]*$")
SEARCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9+_.:@/\- .]*$")


# -----------------------------------------------------------------------------
# General utilities
# -----------------------------------------------------------------------------


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db() -> None:
    con = db()
    con.execute(
        """CREATE TABLE IF NOT EXISTS operations (
            id TEXT PRIMARY KEY,
            manager TEXT NOT NULL,
            package TEXT,
            operation TEXT NOT NULL,
            status TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            stdout TEXT DEFAULT '',
            stderr TEXT DEFAULT '',
            exit_code INTEGER,
            latest_version TEXT,
            size TEXT,
            details_json TEXT DEFAULT '{}'
        )"""
    )
    con.commit()
    con.close()


init_db()


def save_history(
    op_id: str,
    *,
    manager: str,
    package: str | None,
    operation: str,
    status: str,
    started_at: str,
    finished_at: str | None = None,
    stdout: str = "",
    stderr: str = "",
    exit_code: int | None = None,
    latest_version: str = "",
    size: str = "",
    details: dict[str, Any] | None = None,
) -> None:
    con = db()
    con.execute(
        """INSERT OR REPLACE INTO operations
        (id, manager, package, operation, status, started_at, finished_at,
         stdout, stderr, exit_code, latest_version, size, details_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            op_id,
            manager,
            package,
            operation,
            status,
            started_at,
            finished_at,
            stdout,
            stderr,
            exit_code,
            latest_version,
            size,
            json.dumps(details or {}),
        ),
    )
    con.commit()
    con.close()


# -----------------------------------------------------------------------------
# Privilege boundary
# -----------------------------------------------------------------------------


def sudo_ticket_valid() -> bool:
    """Check sudo's existing credential cache without prompting or storing a password."""
    if not shutil.which("sudo"):
        return False
    try:
        result = subprocess.run(
            ["sudo", "-n", "true"],
            capture_output=True,
            text=True,
            timeout=8,
            env={**os.environ, "LC_ALL": "C"},
        )
        return result.returncode == 0
    except Exception:
        return False


def verify_sudo_password(password: str) -> bool:
    """Prime sudo's credential cache once. The password is never stored by the app."""
    if not shutil.which("sudo"):
        return False
    if not password:
        return False
    try:
        result = subprocess.run(
            ["sudo", "-S", "-v"],
            input=f"{password}\n",
            text=True,
            capture_output=True,
            timeout=10,
            env={**os.environ, "LC_ALL": "C"},
        )
        return result.returncode == 0
    except Exception:
        return False


def require_sudo(request: Request) -> None:
    if sudo_ticket_valid():
        return
    raise HTTPException(401, detail="sudo_auth_required")


# -----------------------------------------------------------------------------
# Models
# -----------------------------------------------------------------------------


@dataclass
class Package:
    name: str
    manager: str
    version: str = ""
    latest_version: str = ""
    status: str = "installed"
    size: str = ""
    source: str = ""
    description: str = ""
    system: bool = False
    architecture: str = ""
    installed: bool = True
    source_ref: str = ""


class PackageAction(BaseModel):
    manager: str
    package: str = Field(min_length=1, max_length=256)
    source_ref: str | None = Field(default=None, max_length=256)


class SearchAction(BaseModel):
    query: str = Field(min_length=1, max_length=80)


# -----------------------------------------------------------------------------
# Package manager abstraction
# -----------------------------------------------------------------------------


class Manager:
    name = "unknown"
    display_name = "Unknown"

    def available(self) -> bool:
        return shutil.which(self.name) is not None

    def run(
        self,
        args: list[str],
        *,
        privileged: bool = False,
        timeout: int = 120,
    ) -> tuple[int, str, str]:
        cmd = args
        if privileged:
            if not shutil.which("sudo"):
                return 127, "", "sudo is not installed."
            # Never pass a password through command operations. Re-use sudo's
            # existing credential timestamp after the explicit auth step.
            cmd = ["sudo", "-n", *args]

        try:
            process = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                env={**os.environ, "LC_ALL": "C"},
            )
        except subprocess.TimeoutExpired:
            return 124, "", "Command timed out."
        except FileNotFoundError as exc:
            return 127, "", str(exc)

        if privileged and process.returncode != 0:
            lowered = (process.stderr or "").lower()
            if "a password is required" in lowered or "may not run sudo" in lowered:
                return 401, process.stdout, "sudo_auth_required"

        return process.returncode, process.stdout, process.stderr

    def installed(self) -> list[Package]:
        return []

    def updates(self) -> list[Package]:
        return []

    def search(self, query: str) -> list[Package]:
        return []

    def details(self, package: str) -> Package:
        raise NotImplementedError

    def install(self, package: str, source_ref: str | None = None) -> tuple[int, str, str]:
        raise NotImplementedError

    def update(self, package: str | None = None) -> tuple[int, str, str]:
        raise NotImplementedError

    def remove(self, package: str) -> tuple[int, str, str]:
        raise NotImplementedError

    def cleanup(self) -> tuple[int, str, str]:
        return 0, "No cleanup action is defined for this package manager.", ""


# -----------------------------------------------------------------------------
# APT / Debian
# -----------------------------------------------------------------------------


class AptManager(Manager):
    name = "apt"
    display_name = "APT"

    def installed(self) -> list[Package]:
        if not self.available():
            return []
        fmt = "${binary:Package}\t${Version}\t${Architecture}\t${Priority}\t${Essential}\n"
        code, out, _ = self.run(["dpkg-query", "-W", "-f=" + fmt], timeout=60)
        if code != 0:
            return []
        result: list[Package] = []
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) < 5:
                continue
            name, version, arch, priority, essential = parts[:5]
            system = essential.lower() == "yes" or priority in {"required", "important"}
            result.append(Package(name, self.name, version, system=system, architecture=arch, installed=True))
        return result

    def updates(self) -> list[Package]:
        if not self.available():
            return []
        # This is a read-only simulation of the stable upgrade set. It excludes
        # packages APT would keep back, including phased updates.
        code, out, _ = self.run(["apt-get", "-s", "upgrade"], timeout=90)
        if code != 0:
            return []
        result: list[Package] = []
        pattern = re.compile(
            r"^Inst\s+(\S+)(?:\s+\[[^\]]+\])?\s+\(([^\s\)]+)(?:\s+([^\)]+))?\)$"
        )
        for line in out.splitlines():
            match = pattern.match(line.strip())
            if not match:
                continue
            name, latest, source = match.group(1), match.group(2), (match.group(3) or "").strip()
            result.append(
                Package(
                    name,
                    self.name,
                    status="update_available",
                    latest_version=latest,
                    source=source or "APT repository",
                    installed=True,
                )
            )
        return result

    def search(self, query: str) -> list[Package]:
        if not self.available():
            return []
        code, out, _ = self.run(["apt-cache", "search", query], timeout=90)
        if code != 0:
            return []
        result: list[Package] = []
        for line in out.splitlines()[:80]:
            if " - " in line:
                name, description = line.split(" - ", 1)
            else:
                parts = line.split(maxsplit=1)
                name = parts[0] if parts else ""
                description = parts[1] if len(parts) > 1 else ""
            if PACKAGE_NAME_RE.fullmatch(name):
                result.append(
                    Package(
                        name,
                        self.name,
                        source="APT repositories",
                        description=description,
                        installed=False,
                    )
                )
        return result

    def refresh(self) -> tuple[int, str, str]:
        return self.run(["apt-get", "update"], privileged=True, timeout=300)

    def install(self, package: str) -> tuple[int, str, str]:
        return self.run(["apt-get", "install", "-y", package], privileged=True, timeout=900)

    def update(self, package: str | None = None) -> tuple[int, str, str]:
        if package:
            return self.run(["apt-get", "install", "--only-upgrade", "-y", package], privileged=True, timeout=900)
        return self.run(["apt-get", "upgrade", "-y"], privileged=True, timeout=900)

    def remove(self, package: str) -> tuple[int, str, str]:
        return self.run(["apt-get", "remove", "-y", package], privileged=True, timeout=900)

    def cleanup(self) -> tuple[int, str, str]:
        return self.run(["apt-get", "autoremove", "--purge", "-y"], privileged=True, timeout=900)

    def details(self, package: str) -> Package:
        code, out, _ = self.run(["apt-cache", "show", package], timeout=60)
        if code != 0 or not out:
            return Package(package, self.name, installed=False)
        block = out.split("\n\n", 1)[0]
        fields: dict[str, str] = {}
        current = ""
        for line in block.splitlines():
            if line.startswith((" ", "\t")) and current:
                fields[current] += " " + line.strip()
            elif ": " in line:
                current, value = line.split(": ", 1)
                fields[current] = value.strip()
        installed_map = {p.name: p for p in self.installed()}
        installed = package in installed_map
        return Package(
            package,
            self.name,
            installed_map[package].version if installed else fields.get("Version", ""),
            size=fields.get("Installed-Size", ""),
            source=fields.get("APT-Sources", fields.get("Origin", "APT repository")),
            description=fields.get("Description", ""),
            system=fields.get("Essential", "no").lower() == "yes"
            or fields.get("Priority", "") in {"required", "important"},
            architecture=fields.get("Architecture", ""),
            installed=installed,
        )


# -----------------------------------------------------------------------------
# Snap
# -----------------------------------------------------------------------------


class SnapManager(Manager):
    name = "snap"
    display_name = "Snap"

    def installed(self) -> list[Package]:
        if not self.available():
            return []
        code, out, _ = self.run(["snap", "list"], timeout=60)
        if code != 0:
            return []
        result: list[Package] = []
        for line in out.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2 and parts[0] != "Name":
                result.append(Package(parts[0], self.name, parts[1], source="Snap Store", installed=True))
        return result

    def updates(self) -> list[Package]:
        if not self.available():
            return []
        code, out, _ = self.run(["snap", "refresh", "--list"], timeout=60)
        if code != 0:
            return []
        result: list[Package] = []
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0] not in {"All", "Name", "snapd"}:
                result.append(
                    Package(
                        parts[0],
                        self.name,
                        status="update_available",
                        latest_version=parts[1],
                        source="Snap Store",
                        installed=True,
                    )
                )
        return result

    def search(self, query: str) -> list[Package]:
        if not self.available():
            return []
        code, out, _ = self.run(["snap", "find", query], timeout=90)
        if code != 0:
            return []
        result: list[Package] = []
        lines = out.splitlines()
        for line in lines[1:60]:
            # snap find is human-oriented; the summary column is free-form,
            # so split only enough to capture name/version/publisher/notes.
            parts = line.split()
            if len(parts) < 2 or parts[0].lower() in {"name", "notes"}:
                continue
            name = parts[0]
            if not PACKAGE_NAME_RE.fullmatch(name):
                continue
            version = parts[1]
            publisher = parts[2] if len(parts) > 2 else ""
            description = " ".join(parts[4:]) if len(parts) > 4 else ""
            result.append(
                Package(
                    name,
                    self.name,
                    version=version,
                    latest_version=version,
                    source=publisher or "Snap Store",
                    description=description,
                    installed=False,
                )
            )
        return result

    def install(self, package: str) -> tuple[int, str, str]:
        return self.run(["snap", "install", package], privileged=True, timeout=900)

    def update(self, package: str | None = None) -> tuple[int, str, str]:
        return self.run(["snap", "refresh", *([package] if package else [])], privileged=True, timeout=900)

    def remove(self, package: str) -> tuple[int, str, str]:
        return self.run(["snap", "remove", package], privileged=True, timeout=900)

    def details(self, package: str) -> Package:
        code, out, _ = self.run(["snap", "info", package], timeout=60)
        if code != 0:
            return Package(package, self.name, source="Snap Store", installed=False)
        version = ""
        summary = ""
        publisher = ""
        size = ""
        for line in out.splitlines():
            stripped = line.strip()
            if stripped.startswith("summary:"):
                summary = stripped.split(":", 1)[1].strip()
            elif stripped.startswith("publisher:"):
                publisher = stripped.split(":", 1)[1].strip()
            elif stripped.startswith("installed:"):
                version = stripped.split(":", 1)[1].strip().split()[0]
            elif stripped.startswith("stable:") and not version:
                version = stripped.split(":", 1)[1].strip().split()[0]
            elif stripped.startswith("download-size:"):
                size = stripped.split(":", 1)[1].strip()
        installed = any(p.name == package for p in self.installed())
        return Package(
            package,
            self.name,
            version,
            latest_version=version,
            size=size,
            source=publisher or "Snap Store",
            description=summary,
            installed=installed,
        )


# -----------------------------------------------------------------------------
# Flatpak
# -----------------------------------------------------------------------------


class FlatpakManager(Manager):
    name = "flatpak"
    display_name = "Flatpak"

    def installed(self) -> list[Package]:
        if not self.available():
            return []
        code, out, _ = self.run(["flatpak", "list", "--columns=application,version,arch,origin"], timeout=60)
        if code != 0:
            return []
        result: list[Package] = []
        for line in out.splitlines():
            parts = line.split("\t")
            if parts and parts[0]:
                result.append(
                    Package(
                        parts[0],
                        self.name,
                        parts[1] if len(parts) > 1 else "",
                        source=parts[3] if len(parts) > 3 else "Flatpak remote",
                        architecture=parts[2] if len(parts) > 2 else "",
                        installed=True,
                    )
                )
        return result

    def updates(self) -> list[Package]:
        if not self.available():
            return []
        code, out, _ = self.run(
            ["flatpak", "remote-ls", "--updates", "--columns=application,version,origin"],
            timeout=90,
        )
        if code != 0:
            return []
        result: list[Package] = []
        for line in out.splitlines():
            parts = line.split("\t")
            if parts and parts[0]:
                result.append(
                    Package(
                        parts[0],
                        self.name,
                        status="update_available",
                        latest_version=parts[1] if len(parts) > 1 else "",
                        source=parts[2] if len(parts) > 2 else "Flatpak remote",
                        installed=True,
                    )
                )
        return result

    def search(self, query: str) -> list[Package]:
        if not self.available():
            return []
        code, out, _ = self.run(
            [
                "flatpak",
                "search",
                "--columns=application,name,version,branch,arch,origin,description",
                query,
            ],
            timeout=90,
        )
        if code != 0:
            return []
        result: list[Package] = []
        for line in out.splitlines()[:80]:
            parts = line.split("\t")
            if not parts or not parts[0]:
                continue
            if parts[0].strip().lower() in {"application", "application id", "name"}:
                continue
            result.append(
                Package(
                    parts[0],
                    self.name,
                    version="",
                    latest_version=parts[2] if len(parts) > 2 else "",
                    source=parts[5] if len(parts) > 5 else "Flatpak remote",
                    description=parts[6] if len(parts) > 6 else (parts[1] if len(parts) > 1 else ""),
                    architecture=parts[4] if len(parts) > 4 else "",
                    installed=False,
                    source_ref=parts[5] if len(parts) > 5 else "",
                )
            )
        return result

    def install(self, package: str, source_ref: str | None = None) -> tuple[int, str, str]:
        args = ["flatpak", "install", "-y"]
        if source_ref:
            args.append(source_ref)
        args.append(package)
        return self.run(args, timeout=900)

    def update(self, package: str | None = None) -> tuple[int, str, str]:
        return self.run(["flatpak", "update", "-y", *([package] if package else [])], timeout=900)

    def remove(self, package: str) -> tuple[int, str, str]:
        return self.run(["flatpak", "uninstall", "-y", package], timeout=900)

    def cleanup(self) -> tuple[int, str, str]:
        return self.run(["flatpak", "uninstall", "--unused", "-y"], timeout=600)

    def details(self, package: str) -> Package:
        code, out, _ = self.run(["flatpak", "info", package], timeout=60)
        if code != 0:
            return Package(package, self.name, source="Flatpak remote", installed=False)
        values: dict[str, str] = {}
        for line in out.splitlines():
            if ":" in line:
                key, value = line.split(":", 1)
                values[key.strip().lower()] = value.strip()
        return Package(
            package,
            self.name,
            values.get("version", ""),
            latest_version=values.get("version", ""),
            size=values.get("installed size", values.get("download size", "")),
            source=values.get("origin", values.get("installation", "Flatpak remote")),
            description=values.get("description", ""),
            architecture=values.get("arch", ""),
            installed=True,
        )


# -----------------------------------------------------------------------------
# DNF / Fedora-family
# -----------------------------------------------------------------------------


class DnfManager(Manager):
    name = "dnf"
    display_name = "DNF"

    def installed(self) -> list[Package]:
        if not self.available():
            return []
        code, out, _ = self.run(
            ["rpm", "-qa", "--qf", "%{NAME}\t%{VERSION}-%{RELEASE}\t%{ARCH}\n"], timeout=90
        )
        if code != 0:
            return []
        return [
            Package(parts[0], self.name, parts[1], architecture=parts[2], installed=True)
            for line in out.splitlines()
            if len(parts := line.split("\t")) >= 3 and parts[0]
        ]

    def updates(self) -> list[Package]:
        if not self.available():
            return []
        code, out, _ = self.run(
            ["dnf", "-q", "check-update", "--refresh"], timeout=120
        )
        # dnf check-update uses exit code 100 when updates are available.
        if code not in {0, 100}:
            return []
        result: list[Package] = []
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 3 and parts[0] not in {"Last", "Updating", "Security:"}:
                name, latest, repo = parts[0], parts[1], parts[2]
                if PACKAGE_NAME_RE.fullmatch(name):
                    result.append(Package(name, self.name, status="update_available", latest_version=latest, source=repo, installed=True))
        return result

    def search(self, query: str) -> list[Package]:
        if not self.available():
            return []
        code, out, _ = self.run(
            ["dnf", "repoquery", "--available", "--qf", "%{name}\t%{version}-%{release}\t%{arch}\t%{repoid}\t%{summary}", query],
            timeout=120,
        )
        if code != 0:
            # Fall back to the human search if repoquery is unavailable on this host.
            code, out, _ = self.run(["dnf", "search", query], timeout=120)
            if code != 0:
                return []
        result: list[Package] = []
        for line in out.splitlines()[:80]:
            parts = line.split("\t")
            if len(parts) >= 2 and PACKAGE_NAME_RE.fullmatch(parts[0]):
                result.append(
                    Package(
                        parts[0],
                        self.name,
                        latest_version=parts[1],
                        source=parts[3] if len(parts) > 3 else "DNF repositories",
                        architecture=parts[2] if len(parts) > 2 else "",
                        description=parts[4] if len(parts) > 4 else "",
                        installed=False,
                    )
                )
        return result

    def install(self, package: str) -> tuple[int, str, str]:
        return self.run(["dnf", "install", "-y", package], privileged=True, timeout=900)

    def update(self, package: str | None = None) -> tuple[int, str, str]:
        return self.run(["dnf", "upgrade", "-y", *([package] if package else [])], privileged=True, timeout=900)

    def remove(self, package: str) -> tuple[int, str, str]:
        return self.run(["dnf", "remove", "-y", package], privileged=True, timeout=900)

    def details(self, package: str) -> Package:
        code, out, _ = self.run(["dnf", "info", package], timeout=90)
        if code != 0:
            return Package(package, self.name, source="DNF repository", installed=False)
        values: dict[str, str] = {}
        for line in out.splitlines():
            if ":" in line:
                key, value = line.split(":", 1)
                values[key.strip().lower()] = value.strip()
        installed = any(p.name == package for p in self.installed())
        return Package(
            package,
            self.name,
            values.get("version", ""),
            latest_version=values.get("version", ""),
            size=values.get("size", ""),
            source=values.get("repository", "DNF repository"),
            description=values.get("summary", ""),
            architecture=values.get("architecture", ""),
            installed=installed,
        )


# -----------------------------------------------------------------------------
# Pacman / Arch
# -----------------------------------------------------------------------------


class PacmanManager(Manager):
    name = "pacman"
    display_name = "Pacman"

    def installed(self) -> list[Package]:
        if not self.available():
            return []
        code, out, _ = self.run(["pacman", "-Q"], timeout=90)
        if code != 0:
            return []
        result: list[Package] = []
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 2 and PACKAGE_NAME_RE.fullmatch(parts[0]):
                result.append(Package(parts[0], self.name, parts[1], installed=True))
        return result

    def updates(self) -> list[Package]:
        if not self.available():
            return []
        code, out, _ = self.run(["pacman", "-Qu"], timeout=90)
        if code != 0:
            return []
        result: list[Package] = []
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 4 and PACKAGE_NAME_RE.fullmatch(parts[0]):
                result.append(Package(parts[0], self.name, status="update_available", latest_version=parts[3], source=parts[2], installed=True))
        return result

    def search(self, query: str) -> list[Package]:
        if not self.available():
            return []
        code, out, _ = self.run(["pacman", "-Ss", query], timeout=120)
        if code != 0:
            return []
        result: list[Package] = []
        current_repo = "Arch repositories"
        for line in out.splitlines()[:160]:
            if line and not line.startswith(" ") and "/" in line:
                head = line.split(" ", 1)[0]
                if "/" in head:
                    current_repo, name = head.split("/", 1)
                    version = line.split(" ", 1)[1] if " " in line else ""
                    result.append(Package(name, self.name, latest_version=version, source=current_repo, installed=False))
            elif line.startswith("    ") and result:
                if not result[-1].description:
                    result[-1].description = line.strip()
        return result

    def install(self, package: str) -> tuple[int, str, str]:
        return self.run(["pacman", "-S", "--noconfirm", package], privileged=True, timeout=900)

    def update(self, package: str | None = None) -> tuple[int, str, str]:
        if package:
            return self.run(["pacman", "-S", "--noconfirm", package], privileged=True, timeout=900)
        return self.run(["pacman", "-Syu", "--noconfirm"], privileged=True, timeout=1200)

    def remove(self, package: str) -> tuple[int, str, str]:
        return self.run(["pacman", "-R", "--noconfirm", package], privileged=True, timeout=900)

    def details(self, package: str) -> Package:
        code, out, _ = self.run(["pacman", "-Si", package], timeout=90)
        if code != 0:
            code, out, _ = self.run(["pacman", "-Qi", package], timeout=90)
        if code != 0:
            return Package(package, self.name, source="Arch repository", installed=False)
        values: dict[str, str] = {}
        for line in out.splitlines():
            if " : " in line:
                key, value = line.split(" : ", 1)
                values[key.strip().lower()] = value.strip()
        installed = any(p.name == package for p in self.installed())
        return Package(
            package,
            self.name,
            values.get("version", ""),
            latest_version=values.get("version", ""),
            size=values.get("installed size", values.get("download size", "")),
            source=values.get("repository", "Arch repository"),
            description=values.get("description", ""),
            architecture=values.get("architecture", ""),
            installed=installed,
        )


# -----------------------------------------------------------------------------
# Zypper / openSUSE
# -----------------------------------------------------------------------------


class ZypperManager(Manager):
    name = "zypper"
    display_name = "Zypper"

    def installed(self) -> list[Package]:
        if not self.available():
            return []
        code, out, _ = self.run(["rpm", "-qa", "--qf", "%{NAME}\t%{VERSION}-%{RELEASE}\t%{ARCH}\n"], timeout=90)
        if code != 0:
            return []
        return [
            Package(parts[0], self.name, parts[1], architecture=parts[2], installed=True)
            for line in out.splitlines()
            if len(parts := line.split("\t")) >= 3 and parts[0]
        ]

    def updates(self) -> list[Package]:
        if not self.available():
            return []
        code, out, _ = self.run(["zypper", "--non-interactive", "list-updates"], timeout=120)
        if code != 0:
            return []
        result: list[Package] = []
        for line in out.splitlines():
            if "|" not in line or line.strip().startswith(("S |", "--")):
                continue
            cols = [c.strip() for c in line.split("|")]
            if len(cols) >= 6 and PACKAGE_NAME_RE.fullmatch(cols[1]):
                result.append(Package(cols[1], self.name, status="update_available", latest_version=cols[4], source=cols[0] or "Zypper repository", installed=True))
        return result

    def search(self, query: str) -> list[Package]:
        if not self.available():
            return []
        code, out, _ = self.run(["zypper", "--non-interactive", "search", "--match-words", query], timeout=120)
        if code != 0:
            return []
        result: list[Package] = []
        for line in out.splitlines()[3:140]:
            if "|" not in line:
                continue
            cols = [c.strip() for c in line.split("|")]
            if len(cols) >= 4 and PACKAGE_NAME_RE.fullmatch(cols[1]):
                result.append(Package(cols[1], self.name, latest_version=cols[2], source=cols[0] or "Zypper repository", description=cols[3], installed=False))
        return result

    def install(self, package: str) -> tuple[int, str, str]:
        return self.run(["zypper", "--non-interactive", "install", "--no-confirm", package], privileged=True, timeout=900)

    def update(self, package: str | None = None) -> tuple[int, str, str]:
        if package:
            return self.run(["zypper", "--non-interactive", "update", "--no-confirm", package], privileged=True, timeout=900)
        return self.run(["zypper", "--non-interactive", "update", "--no-confirm"], privileged=True, timeout=1200)

    def remove(self, package: str) -> tuple[int, str, str]:
        return self.run(["zypper", "--non-interactive", "remove", "--no-confirm", package], privileged=True, timeout=900)

    def details(self, package: str) -> Package:
        code, out, _ = self.run(["zypper", "info", package], timeout=90)
        if code != 0:
            return Package(package, self.name, source="Zypper repository", installed=False)
        values: dict[str, str] = {}
        for line in out.splitlines():
            if " : " in line:
                key, value = line.split(" : ", 1)
                values[key.strip().lower()] = value.strip()
        installed = any(p.name == package for p in self.installed())
        return Package(
            package,
            self.name,
            values.get("version", ""),
            latest_version=values.get("version", ""),
            size=values.get("installed size", ""),
            source=values.get("repository", "Zypper repository"),
            description=values.get("summary", values.get("description", "")),
            architecture=values.get("architecture", ""),
            installed=installed,
        )


MANAGERS: dict[str, Manager] = {
    "apt": AptManager(),
    "snap": SnapManager(),
    "flatpak": FlatpakManager(),
    "dnf": DnfManager(),
    "pacman": PacmanManager(),
    "zypper": ZypperManager(),
}


# -----------------------------------------------------------------------------
# Package aggregation
# -----------------------------------------------------------------------------


def manager_state() -> dict[str, dict[str, Any]]:
    state: dict[str, dict[str, Any]] = {}
    for name, manager in MANAGERS.items():
        available = manager.available()
        state[name] = {
            "available": available,
            "display_name": manager.display_name,
            "installed": len(manager.installed()) if available else 0,
        }
    return state


def all_packages() -> list[Package]:
    result: list[Package] = []
    for manager in MANAGERS.values():
        if manager.available():
            result.extend(manager.installed())
    return result


def current_update_map() -> dict[tuple[str, str], Package]:
    result: dict[tuple[str, str], Package] = {}
    for manager in MANAGERS.values():
        if manager.available():
            for package in manager.updates():
                result[(package.manager, package.name)] = package
    return result


def remember_search_results(items: list[Package]) -> None:
    with _search_catalog_lock:
        for item in items:
            _search_catalog[(item.manager, item.name)] = asdict(item)


def get_remembered_search(manager: str, package: str) -> dict[str, Any] | None:
    with _search_catalog_lock:
        item = _search_catalog.get((manager, package))
        return dict(item) if item else None


def enrich_package(pkg: Package, updates: dict[tuple[str, str], Package], installed_map: dict[tuple[str, str], Package]) -> Package:
    installed = (pkg.manager, pkg.name) in installed_map
    update = updates.get((pkg.manager, pkg.name))
    if installed:
        pkg.installed = True
        pkg.version = installed_map[(pkg.manager, pkg.name)].version
        pkg.system = installed_map[(pkg.manager, pkg.name)].system
        pkg.architecture = installed_map[(pkg.manager, pkg.name)].architecture or pkg.architecture
    else:
        pkg.installed = False
    if update:
        pkg.status = "update_available"
        pkg.latest_version = update.latest_version
        pkg.source = update.source or pkg.source
    elif pkg.latest_version:
        if not installed:
            pkg.status = "available"
    elif installed:
        pkg.latest_version = pkg.version
        pkg.status = "installed"
    return pkg


# -----------------------------------------------------------------------------
# Background operation framework
# -----------------------------------------------------------------------------


def begin_operation(manager: str, package: str | None, operation: str, latest_version: str = "") -> str:
    op_id = str(uuid.uuid4())
    started = now_iso()
    state = {
        "id": op_id,
        "manager": manager,
        "package": package,
        "operation": operation,
        "status": "running",
        "progress": 5,
        "message": "Starting…",
        "started_at": started,
        "finished_at": None,
        "stdout": "",
        "stderr": "",
        "code": None,
        "latest_version": latest_version,
    }
    with _operations_lock:
        _operations[op_id] = state
    save_history(
        op_id,
        manager=manager,
        package=package,
        operation=operation,
        status="running",
        started_at=started,
        latest_version=latest_version,
    )
    return op_id


def update_operation(op_id: str, **changes: Any) -> None:
    with _operations_lock:
        if op_id in _operations:
            _operations[op_id].update(changes)


def run_async(
    op_id: str,
    fn,
    *,
    manager: str,
    package: str | None,
    operation: str,
    latest_version: str = "",
) -> None:
    def worker() -> None:
        try:
            update_operation(op_id, progress=15, message=f"Running {manager.upper()}…")
            code, out, err = fn()
            success = code == 0
            finished = now_iso()
            final_status = "success" if success else "failed"
            update_operation(
                op_id,
                progress=100,
                message="Completed" if success else "Failed",
                status=final_status,
                finished_at=finished,
                stdout=out,
                stderr=err,
                code=code,
            )
            save_history(
                op_id,
                manager=manager,
                package=package,
                operation=operation,
                status=final_status,
                started_at=_operations[op_id]["started_at"],
                finished_at=finished,
                stdout=out,
                stderr=err,
                exit_code=code,
                latest_version=latest_version,
            )
        except Exception as exc:
            finished = now_iso()
            update_operation(op_id, progress=100, message="Failed", status="failed", finished_at=finished, stderr=str(exc), code=1)
            save_history(
                op_id,
                manager=manager,
                package=package,
                operation=operation,
                status="failed",
                started_at=_operations[op_id]["started_at"],
                finished_at=finished,
                stderr=str(exc),
                exit_code=1,
                latest_version=latest_version,
            )

    _executor.submit(worker)


# -----------------------------------------------------------------------------
# HTTP routes
# -----------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request=request, name="index.html", context={"request": request})


@app.get("/api/system")
def system():
    return {
        "hostname": os.uname().nodename,
        "platform": os.uname().sysname,
        "release": os.uname().release,
        "managers": manager_state(),
    }


@app.get("/api/packages")
def packages(manager: str | None = None, q: str | None = None, hide_system: bool = False, limit: int = 1000):
    if manager and manager not in MANAGERS:
        raise HTTPException(400, "Unsupported package manager")
    items = MANAGERS[manager].installed() if manager else all_packages()
    installed_map = {(p.manager, p.name): p for p in all_packages()}
    updates = current_update_map()
    for package in items:
        enrich_package(package, updates, installed_map)
    if q:
        items = [p for p in items if q.lower() in p.name.lower()]
    if hide_system:
        items = [p for p in items if not p.system]
    items.sort(key=lambda p: (p.manager, p.name.lower()))
    return {"count": len(items), "packages": [asdict(p) for p in items[: max(1, min(limit, 5000))]]}


@app.get("/api/search")
def search_packages(
    q: str = Query(min_length=1, max_length=80),
    manager: str | None = None,
):
    query = q.strip()
    if not SEARCH_RE.fullmatch(query):
        raise HTTPException(400, "Search contains unsupported characters")
    selected = [manager] if manager else list(MANAGERS.keys())
    for name in selected:
        if name not in MANAGERS:
            raise HTTPException(400, f"Unsupported package manager: {name}")

    installed_map = {(p.manager, p.name): p for p in all_packages()}
    updates = current_update_map()
    results: list[Package] = []
    errors: list[dict[str, str]] = []

    for name in selected:
        mgr = MANAGERS[name]
        if not mgr.available():
            continue
        try:
            found = mgr.search(query)
            for package in found:
                enrich_package(package, updates, installed_map)
                results.append(package)
        except Exception as exc:
            errors.append({"manager": name, "error": str(exc)})

    # De-duplicate exact manager/package pairs, retaining richer metadata.
    unique: dict[tuple[str, str, str], Package] = {}
    for package in results:
        key = (package.manager, package.name, package.source)
        existing = unique.get(key)
        if existing is None:
            unique[key] = package
        else:
            if len(package.description) > len(existing.description):
                existing.description = package.description
            if package.source:
                existing.source = package.source
            if package.latest_version:
                existing.latest_version = package.latest_version

    final = list(unique.values())
    final.sort(key=lambda p: (p.manager, p.name.lower()))
    remember_search_results(final)
    return {
        "query": query,
        "count": len(final),
        "managers": sorted({p.manager for p in final}),
        "results": [asdict(p) for p in final[:300]],
        "errors": errors,
    }


@app.get("/api/packages/{manager}/{package:path}")
def package_details(manager: str, package: str):
    if manager not in MANAGERS:
        raise HTTPException(400, "Unsupported package manager")
    if not PACKAGE_NAME_RE.fullmatch(package):
        raise HTTPException(400, "Invalid package name")

    installed_map = {(p.manager, p.name): p for p in MANAGERS[manager].installed()}
    update = current_update_map().get((manager, package))

    detail: Package | None = None
    try:
        detail = MANAGERS[manager].details(package)
    except Exception:
        detail = None

    if detail is None or (not detail.description and not detail.version and not detail.source):
        remembered = get_remembered_search(manager, package)
        if remembered:
            detail = Package(**remembered)
        elif detail is None:
            detail = Package(package, manager, installed=False)

    if (manager, package) in installed_map:
        installed = installed_map[(manager, package)]
        detail.installed = True
        detail.version = installed.version
        detail.system = installed.system
        detail.architecture = installed.architecture or detail.architecture
    else:
        detail.installed = False

    if update:
        detail.status = "update_available"
        detail.latest_version = update.latest_version
        if update.source:
            detail.source = update.source
    elif detail.installed:
        detail.latest_version = detail.latest_version or detail.version
        detail.status = "installed"
    else:
        detail.status = "available"

    return {"package": asdict(detail)}


@app.get("/api/auth/sudo-status")
def sudo_status():
    return {"authenticated": sudo_ticket_valid(), "passwordless": sudo_ticket_valid() and False}


@app.post("/api/auth/sudo-verify")
def sudo_verify(payload: dict[str, str]):
    password = payload.get("password", "")
    if verify_sudo_password(password):
        return {"success": True, "message": "Authentication successful."}
    raise HTTPException(401, "Incorrect sudo password. Please try again.")


@app.post("/api/auth/sudo-clear")
def sudo_clear():
    # Expire sudo's ticket for the current user where possible.
    if shutil.which("sudo"):
        subprocess.run(["sudo", "-k"], capture_output=True, text=True, timeout=8)
    return {"success": True}


@app.post("/api/refresh")
def refresh_package_indexes(request: Request):
    require_sudo(request)
    if not MANAGERS["apt"].available():
        raise HTTPException(400, "APT is not available on this host")
    op_id = begin_operation("apt", None, "refresh")
    run_async(op_id, MANAGERS["apt"].refresh, manager="apt", package=None, operation="refresh")
    return {"operation_id": op_id}


@app.get("/api/updates")
def updates():
    items = list(current_update_map().values())
    items.sort(key=lambda p: (p.manager, p.name.lower()))
    return {"count": len(items), "updates": [asdict(p) for p in items]}


@app.get("/api/history")
def history(limit: int = 50):
    con = db()
    rows = con.execute(
        """SELECT id, manager, package, operation, status, started_at,
                  finished_at, stdout, stderr, exit_code, latest_version
           FROM operations
           WHERE status='success' AND operation IN ('update','update-all','install','remove')
           ORDER BY COALESCE(finished_at, started_at) DESC
           LIMIT ?""",
        (max(1, min(limit, 100)),),
    ).fetchall()
    con.close()
    return {"items": [dict(row) for row in rows]}


def validate_package_action(action: PackageAction) -> Manager:
    if action.manager not in MANAGERS:
        raise HTTPException(400, "Unsupported package manager")
    if not PACKAGE_NAME_RE.fullmatch(action.package):
        raise HTTPException(400, "Invalid package name")
    if action.source_ref and not re.fullmatch(r"[A-Za-z0-9._-]{1,256}", action.source_ref):
        raise HTTPException(400, "Invalid package source")
    manager = MANAGERS[action.manager]
    if not manager.available():
        raise HTTPException(400, f"{manager.display_name} is not installed on this host")
    return manager


@app.post("/api/packages/install")
def install_package(request: Request, action: PackageAction):
    manager = validate_package_action(action)
    require_sudo(request) if action.manager in {"apt", "snap", "dnf", "pacman", "zypper"} else None
    op_id = begin_operation(action.manager, action.package, "install")
    run_async(
        op_id,
        lambda: manager.install(action.package, action.source_ref) if action.manager == "flatpak" else manager.install(action.package),
        manager=action.manager,
        package=action.package,
        operation="install",
    )
    return {"operation_id": op_id}


@app.post("/api/packages/update")
def update_package(request: Request, action: PackageAction):
    manager = validate_package_action(action)
    if action.manager in {"apt", "snap", "dnf", "pacman", "zypper"}:
        require_sudo(request)
    update = current_update_map().get((action.manager, action.package))
    if not update:
        raise HTTPException(409, "No stable update is currently available for this package.")
    op_id = begin_operation(action.manager, action.package, "update", latest_version=update.latest_version)
    run_async(
        op_id,
        lambda: manager.update(action.package),
        manager=action.manager,
        package=action.package,
        operation="update",
        latest_version=update.latest_version,
    )
    return {"operation_id": op_id, "latest_version": update.latest_version}


@app.post("/api/packages/remove")
def remove_package(request: Request, action: PackageAction):
    manager = validate_package_action(action)
    if action.manager in {"apt", "snap", "dnf", "pacman", "zypper"}:
        require_sudo(request)
    installed = {(p.manager, p.name): p for p in manager.installed()}
    if (action.manager, action.package) not in installed:
        raise HTTPException(409, "Package is not currently installed.")
    op_id = begin_operation(action.manager, action.package, "remove")
    run_async(
        op_id,
        lambda: manager.remove(action.package),
        manager=action.manager,
        package=action.package,
        operation="remove",
    )
    return {"operation_id": op_id}


@app.post("/api/update-all")
def update_all(request: Request):
    require_sudo(request)
    op_id = begin_operation("all", None, "update-all")

    def worker() -> tuple[int, str, str]:
        outputs: list[str] = []
        errors: list[str] = []
        last_code = 0
        available = [(name, mgr) for name, mgr in MANAGERS.items() if mgr.available()]
        for index, (name, manager) in enumerate(available, start=1):
            update_operation(op_id, progress=10 + int(index * 80 / max(1, len(available))), message=f"Updating {manager.display_name}…")
            code, out, err = manager.update()
            outputs.append(f"[{manager.display_name}]\n{out}")
            if err:
                errors.append(f"[{manager.display_name}]\n{err}")
            if code != 0:
                last_code = code
        return last_code, "\n".join(outputs), "\n".join(errors)

    run_async(op_id, worker, manager="all", package=None, operation="update-all")
    return {"operation_id": op_id}


@app.post("/api/maintenance/apt-autoremove")
def apt_autoremove(request: Request):
    require_sudo(request)
    manager = MANAGERS["apt"]
    if not manager.available():
        raise HTTPException(400, "APT is not available on this host")
    op_id = begin_operation("apt", None, "autoremove")
    run_async(op_id, manager.cleanup, manager="apt", package=None, operation="autoremove")
    return {"operation_id": op_id}


@app.post("/api/maintenance/flatpak-unused")
def flatpak_unused():
    manager = MANAGERS["flatpak"]
    if not manager.available():
        raise HTTPException(400, "Flatpak is not installed on this host")
    op_id = begin_operation("flatpak", None, "flatpak-unused")
    run_async(op_id, manager.cleanup, manager="flatpak", package=None, operation="flatpak-unused")
    return {"operation_id": op_id}


@app.get("/api/operations/{op_id}")
def operation_status(op_id: str):
    with _operations_lock:
        item = _operations.get(op_id)
    if not item:
        con = db()
        row = con.execute("SELECT * FROM operations WHERE id=?", (op_id,)).fetchone()
        con.close()
        if not row:
            raise HTTPException(404, "Operation not found")
        return {"operation": dict(row) | {"progress": 100, "message": row["status"].title()}}
    return {"operation": item}

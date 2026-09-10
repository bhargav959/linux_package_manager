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

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "history.sqlite3"

app = FastAPI(title="Linux Package Manager", version="0.3.0")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="pkg-op")
_operations: dict[str, dict[str, Any]] = {}
_operations_lock = threading.Lock()
_sudo_password: str | None = None
_sudo_password_lock = threading.Lock()


def get_sudo_password() -> str | None:
    with _sudo_password_lock:
        return _sudo_password


def set_sudo_password(password: str | None) -> None:
    global _sudo_password
    with _sudo_password_lock:
        _sudo_password = password


def check_sudo_non_interactive() -> bool:
    if not shutil.which("sudo"):
        return False
    res = subprocess.run(["sudo", "-n", "true"], capture_output=True, text=True)
    return res.returncode == 0


def verify_sudo_password(password: str) -> bool:
    if not shutil.which("sudo"):
        return False
    try:
        p = subprocess.run(
            ["sudo", "-S", "-v"],
            input=f"{password}\n",
            text=True,
            capture_output=True,
            timeout=10,
            env={**os.environ, "LC_ALL": "C"},
        )
        return p.returncode == 0
    except Exception:
        return False


def check_privileged_auth(request: Request, body_password: str | None = None) -> str | None:
    header_pwd = request.headers.get("X-Sudo-Password")
    pwd = body_password or header_pwd or get_sudo_password()
    if pwd and verify_sudo_password(pwd):
        set_sudo_password(pwd)
        return pwd
    raise HTTPException(401, detail="sudo_auth_required")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def db() -> sqlite3.Connection:
    con = db_conn() if "db_conn" in globals() else sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db() -> None:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
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


class PackageAction(BaseModel):
    manager: str = Field(pattern="^(apt|snap|flatpak)$")
    package: str = Field(
        min_length=1,
        max_length=256,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9+_.:@/-]*$",
    )
    sudo_password: str | None = None


class SudoVerifyAction(BaseModel):
    password: str


class Manager:
    name = "unknown"

    def available(self) -> bool:
        return shutil.which(self.name) is not None

    def run(
        self,
        args: list[str],
        privileged: bool = False,
        timeout: int = 120,
        sudo_password: str | None = None,
    ) -> tuple[int, str, str]:
        input_data = None
        if privileged:
            if not shutil.which("sudo"):
                return 127, "", "sudo is not installed."
            pwd = sudo_password or get_sudo_password()
            if not pwd:
                return 401, "", "sudo_auth_required"
            cmd = ["sudo", "-S", "-p", "", *args]
            input_data = f"{pwd}\n"
        else:
            cmd = args

        try:
            p = subprocess.run(
                cmd,
                input=input_data,
                text=True,
                capture_output=True,
                timeout=timeout,
                env={**os.environ, "LC_ALL": "C"},
            )
            if privileged and p.returncode != 0:
                err_lower = (p.stderr or "").lower()
                if "a password is required" in err_lower or "incorrect password" in err_lower:
                    return 401, p.stdout, p.stderr or "sudo_auth_required"
            return p.returncode, p.stdout, p.stderr
        except subprocess.TimeoutExpired:
            return 124, "", "Command timed out."
        except FileNotFoundError as exc:
            return 127, "", str(exc)

    def installed(self) -> list[Package]:
        return []

    def updates(self) -> list[Package]:
        return []

    def details(self, package: str) -> Package:
        raise NotImplementedError


class AptManager(Manager):
    name = "apt"

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
            result.append(Package(name, "apt", version, system=system, architecture=arch))
        return result

    def updates(self) -> list[Package]:
        if not self.available():
            return []
        # APT's simulated upgrade set excludes updates that would be kept back,
        # including phased updates. It is read-only and requires no authentication.
        code, out, _ = self.run(["apt-get", "-s", "upgrade"], timeout=90)
        if code != 0:
            return []
        result: list[Package] = []
        # Example: Inst package [old] (new repo)
        pattern = re.compile(r"^Inst\s+(\S+)(?:\s+\[[^\]]+\])?\s+\(([^\s)]+)(?:\s+([^\)]+))?\)$")
        for line in out.splitlines():
            m = pattern.match(line.strip())
            if not m:
                continue
            name, latest, source = m.group(1), m.group(2), m.group(3) or ""
            result.append(Package(name, "apt", status="update_available", latest_version=latest, source=source))
        return result

    def refresh(self) -> tuple[int, str, str]:
        return self.run(["apt-get", "update"], privileged=True, timeout=300)

    def update(self, package: str | None = None) -> tuple[int, str, str]:
        args = ["apt-get", "install", "--only-upgrade", "-y"]
        if package:
            args.append(package)
        else:
            args = ["apt-get", "upgrade", "-y"]
        return self.run(args, privileged=True, timeout=900)

    def cleanup(self) -> tuple[int, str, str]:
        return self.run(["apt-get", "autoremove", "--purge", "-y"], privileged=True, timeout=900)

    def details(self, package: str) -> Package:
        code, out, _ = self.run(["apt-cache", "show", package], timeout=60)
        if code != 0 or not out:
            return Package(package, "apt")
        block = out.split("\n\n", 1)[0]
        fields: dict[str, str] = {}
        current = ""
        for line in block.splitlines():
            if line.startswith((" ", "\t")) and current:
                fields[current] += " " + line.strip()
            elif ": " in line:
                current, value = line.split(": ", 1)
                fields[current] = value.strip()
        return Package(
            package,
            "apt",
            fields.get("Version", ""),
            size=fields.get("Installed-Size", ""),
            source=fields.get("APT-Sources", fields.get("Origin", "")),
            description=fields.get("Description", ""),
            system=fields.get("Essential", "no").lower() == "yes" or fields.get("Priority", "") in {"required", "important"},
            architecture=fields.get("Architecture", ""),
        )


class SnapManager(Manager):
    name = "snap"

    def installed(self) -> list[Package]:
        if not self.available():
            return []
        code, out, _ = self.run(["snap", "list"], timeout=60)
        if code != 0:
            return []
        result: list[Package] = []
        for line in out.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2:
                result.append(Package(parts[0], "snap", parts[1], architecture=""))
        return result

    def updates(self) -> list[Package]:
        if not self.available():
            return []
        code, out, _ = self.run(["snap", "refresh", "--list"], timeout=60)
        if code != 0:
            return []
        result: list[Package] = []
        for line in out.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2 and parts[0] not in {"All", "Name"}:
                result.append(Package(parts[0], "snap", status="update_available", latest_version=parts[1], source="snap"))
        return result

    def update(self, package: str | None = None) -> tuple[int, str, str]:
        return self.run(["snap", "refresh", *( [package] if package else [] )], privileged=True, timeout=900)

    def cleanup(self) -> tuple[int, str, str]:
        return 0, "Snap cleanup is intentionally conservative. Review disabled revisions before removing them.", ""

    def details(self, package: str) -> Package:
        code, out, _ = self.run(["snap", "info", package], timeout=60)
        if code != 0:
            return Package(package, "snap")
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
            elif stripped.startswith("size:"):
                size = stripped.split(":", 1)[1].strip()
        return Package(package, "snap", version, size=size, source=publisher or "snap", description=summary)


class FlatpakManager(Manager):
    name = "flatpak"

    def installed(self) -> list[Package]:
        if not self.available():
            return []
        code, out, _ = self.run(["flatpak", "list", "--columns=application,version"], timeout=60)
        if code != 0:
            return []
        result: list[Package] = []
        for line in out.splitlines():
            parts = line.split("\t", 1)
            if parts and parts[0]:
                result.append(Package(parts[0], "flatpak", parts[1] if len(parts) > 1 else ""))
        return result

    def updates(self) -> list[Package]:
        if not self.available():
            return []
        code, out, _ = self.run(["flatpak", "remote-ls", "--updates", "--columns=application,version"], timeout=90)
        if code != 0:
            return []
        result: list[Package] = []
        for line in out.splitlines():
            parts = line.split("\t", 1)
            if parts and parts[0]:
                result.append(Package(parts[0], "flatpak", status="update_available", latest_version=parts[1] if len(parts) > 1 else "", source="flatpak"))
        return result

    def update(self, package: str | None = None) -> tuple[int, str, str]:
        if not self.available():
            return 0, "Flatpak is not installed on this host.", ""
        return self.run(["flatpak", "update", "-y", *( [package] if package else [] )], timeout=900)

    def cleanup(self) -> tuple[int, str, str]:
        if not self.available():
            return 0, "Flatpak is not installed on this host.", ""
        return self.run(["flatpak", "uninstall", "--unused", "-y"], timeout=600)

    def details(self, package: str) -> Package:
        code, out, _ = self.run(["flatpak", "info", package], timeout=60)
        if code != 0:
            return Package(package, "flatpak")
        values: dict[str, str] = {}
        for line in out.splitlines():
            if ":" in line:
                key, value = line.split(":", 1)
                values[key.strip().lower()] = value.strip()
        return Package(
            package,
            "flatpak",
            values.get("version", ""),
            size=values.get("installed size", values.get("download size", "")),
            source=values.get("origin", values.get("installation", "flatpak")),
            description=values.get("description", ""),
            architecture=values.get("arch", ""),
        )


MANAGERS: dict[str, Manager] = {"apt": AptManager(), "snap": SnapManager(), "flatpak": FlatpakManager()}


def manager_state() -> dict[str, dict[str, Any]]:
    return {
        name: {"available": mgr.available(), "installed": len(mgr.installed()) if mgr.available() else 0}
        for name, mgr in MANAGERS.items()
    }


def all_packages() -> list[Package]:
    items: list[Package] = []
    for mgr in MANAGERS.values():
        items.extend(mgr.installed())
    return items


def current_update_map() -> dict[tuple[str, str], Package]:
    result: dict[tuple[str, str], Package] = {}
    for mgr in MANAGERS.values():
        if mgr.available():
            for p in mgr.updates():
                result[(p.manager, p.name)] = p
    return result


def command_result(code: int, out: str, err: str) -> dict[str, Any]:
    return {"success": code == 0, "code": code, "stdout": out, "stderr": err}


def save_history(op_id: str, *, manager: str, package: str | None, operation: str, status: str, started_at: str,
                 finished_at: str | None = None, stdout: str = "", stderr: str = "", exit_code: int | None = None,
                 latest_version: str = "", size: str = "", details: dict[str, Any] | None = None) -> None:
    con = db()
    con.execute(
        """INSERT OR REPLACE INTO operations
        (id, manager, package, operation, status, started_at, finished_at, stdout, stderr, exit_code, latest_version, size, details_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (op_id, manager, package, operation, status, started_at, finished_at, stdout, stderr, exit_code, latest_version, size, json.dumps(details or {})),
    )
    con.commit()
    con.close()


def update_operation(op_id: str, **changes: Any) -> None:
    with _operations_lock:
        if op_id in _operations:
            _operations[op_id].update(changes)


def begin_operation(manager: str, package: str | None, operation: str) -> str:
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
    }
    with _operations_lock:
        _operations[op_id] = state
    save_history(op_id, manager=manager, package=package, operation=operation, status="running", started_at=started)
    return op_id


def run_async(op_id: str, fn, *, manager: str, package: str | None, operation: str) -> None:
    def worker() -> None:
        try:
            update_operation(op_id, progress=15, message="Running package manager…")
            code, out, err = fn()
            success = code == 0
            finished = now_iso()
            update_operation(op_id, progress=100 if success else 100, message="Completed" if success else "Failed",
                             status="success" if success else "failed", finished_at=finished, stdout=out, stderr=err, code=code)
            save_history(op_id, manager=manager, package=package, operation=operation,
                         status="success" if success else "failed", started_at=_operations[op_id]["started_at"],
                         finished_at=finished, stdout=out, stderr=err, exit_code=code)
        except Exception as exc:  # defensive boundary for background jobs
            finished = now_iso()
            update_operation(op_id, progress=100, message="Failed", status="failed", finished_at=finished,
                             stderr=str(exc), code=1)
            save_history(op_id, manager=manager, package=package, operation=operation, status="failed",
                         started_at=_operations[op_id]["started_at"], finished_at=finished, stderr=str(exc), exit_code=1)
    _executor.submit(worker)


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request=request, name="index.html", context={"request": request})


@app.get("/api/system")
def system():
    return {"hostname": os.uname().nodename, "platform": os.uname().sysname, "release": os.uname().release, "managers": manager_state()}


@app.get("/api/packages")
def packages(manager: str | None = None, q: str | None = None, hide_system: bool = False, limit: int = 1000):
    if manager and manager not in MANAGERS:
        raise HTTPException(400, "Unsupported package manager")
    items = MANAGERS[manager].installed() if manager else all_packages()
    updates = current_update_map()
    for p in items:
        key = (p.manager, p.name)
        if key in updates:
            p.status = "update_available"
            p.latest_version = updates[key].latest_version
            p.source = updates[key].source
        elif not p.latest_version:
            p.latest_version = p.version
    if q:
        query = q.lower()
        items = [p for p in items if query in p.name.lower()]
    if hide_system:
        items = [p for p in items if not p.system]
    items.sort(key=lambda p: (p.manager, p.name.lower()))
    return {"count": len(items), "packages": [asdict(p) for p in items[: max(1, min(limit, 5000))]]}


@app.get("/api/packages/{manager}/{package:path}")
def package_details(manager: str, package: str):
    if manager not in MANAGERS:
        raise HTTPException(400, "Unsupported package manager")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9+_.:@/-]*", package):
        raise HTTPException(400, "Invalid package name")
    detail = MANAGERS[manager].details(package)
    update = current_update_map().get((manager, package))
    if update:
        detail.latest_version = update.latest_version
        detail.status = "update_available"
        if update.source:
            detail.source = update.source
    elif not detail.latest_version:
        detail.latest_version = detail.version
    return {"package": asdict(detail)}


@app.get("/api/auth/sudo-status")
def sudo_status():
    if check_sudo_non_interactive():
        return {"authenticated": True, "passwordless": True}
    cached_pwd = get_sudo_password()
    if cached_pwd and verify_sudo_password(cached_pwd):
        return {"authenticated": True, "passwordless": False}
    return {"authenticated": False, "passwordless": False}


@app.post("/api/auth/sudo-verify")
def sudo_verify(action: SudoVerifyAction):
    if verify_sudo_password(action.password):
        set_sudo_password(action.password)
        return {"success": True, "message": "Authentication successful."}
    raise HTTPException(401, "Incorrect sudo password. Please try again.")


@app.post("/api/auth/sudo-clear")
def sudo_clear():
    set_sudo_password(None)
    return {"success": True}


@app.post("/api/refresh")
def refresh_package_indexes(request: Request):
    check_privileged_auth(request)
    op_id = begin_operation("apt", None, "refresh")
    run_async(op_id, MANAGERS["apt"].refresh, manager="apt", package=None, operation="refresh")
    return {"operation_id": op_id}


@app.get("/api/updates")
def updates():
    items = list(current_update_map().values())
    items.sort(key=lambda p: (p.manager, p.name.lower()))
    return {"count": len(items), "updates": [asdict(p) for p in items]}


@app.get("/api/history")
def history(limit: int = 25):
    con = db()
    rows = con.execute(
        "SELECT id, manager, package, operation, status, started_at, finished_at, stdout, stderr, exit_code FROM operations WHERE status='success' AND operation IN ('update','update-all') ORDER BY COALESCE(finished_at, started_at) DESC LIMIT ?",
        (max(1, min(limit, 100)),),
    ).fetchall()
    con.close()
    return {"items": [dict(r) for r in rows]}


@app.post("/api/packages/update")
def update_package(request: Request, action: PackageAction):
    if action.manager in {"apt", "snap"}:
        check_privileged_auth(request, action.sudo_password)
    updates = current_update_map()
    selected = updates.get((action.manager, action.package))
    if not selected:
        raise HTTPException(409, "No stable update is currently available for this package.")
    op_id = begin_operation(action.manager, action.package, "update")
    run_async(op_id, lambda: MANAGERS[action.manager].update(action.package), manager=action.manager, package=action.package, operation="update")
    return {"operation_id": op_id, "latest_version": selected.latest_version}


@app.post("/api/packages/remove")
def remove_package(request: Request, action: PackageAction):
    if action.manager in {"apt", "snap"}:
        check_privileged_auth(request, action.sudo_password)
    mgr = MANAGERS[action.manager]
    if action.manager == "apt":
        fn = lambda: mgr.run(["apt-get", "remove", "-y", action.package], privileged=True, timeout=600)
    elif action.manager == "snap":
        fn = lambda: mgr.run(["snap", "remove", action.package], privileged=True, timeout=600)
    else:
        fn = lambda: mgr.run(["flatpak", "uninstall", "-y", action.package], timeout=600)
    op_id = begin_operation(action.manager, action.package, "remove")
    run_async(op_id, fn, manager=action.manager, package=action.package, operation="remove")
    return {"operation_id": op_id}


@app.post("/api/update-all")
def update_all(request: Request):
    check_privileged_auth(request)
    op_id = begin_operation("all", None, "update-all")

    def worker() -> tuple[int, str, str]:
        outputs: list[str] = []
        errors: list[str] = []
        last_code = 0
        available = [(name, mgr) for name, mgr in MANAGERS.items() if mgr.available()]
        if not available:
            return 0, "No supported package managers are installed.", ""
        for i, (name, mgr) in enumerate(available, start=1):
            update_operation(op_id, progress=10 + int(i * 80 / len(available)), message=f"Updating {name.upper()}…")
            if name == "apt":
                code, out, err = mgr.update()
            elif name == "snap":
                code, out, err = mgr.update()
            else:
                code, out, err = mgr.update()
            outputs.append(f"[{name}]\n{out}")
            errors.append(f"[{name}]\n{err}" if err else "")
            if code != 0:
                last_code = code
        return last_code, "\n".join(outputs), "\n".join(x for x in errors if x)

    run_async(op_id, worker, manager="all", package=None, operation="update-all")
    return {"operation_id": op_id}


@app.post("/api/maintenance/apt-autoremove")
def apt_autoremove(request: Request):
    check_privileged_auth(request)
    op_id = begin_operation("apt", None, "autoremove")
    run_async(op_id, MANAGERS["apt"].cleanup, manager="apt", package=None, operation="autoremove")
    return {"operation_id": op_id}


@app.post("/api/maintenance/flatpak-unused")
def flatpak_unused():
    op_id = begin_operation("flatpak", None, "flatpak-unused")
    run_async(op_id, MANAGERS["flatpak"].cleanup, manager="flatpak", package=None, operation="flatpak-unused")
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

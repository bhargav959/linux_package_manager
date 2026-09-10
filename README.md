# Linux Package Manager

Local web application for managing APT, Snap, and Flatpak packages from one interface.

## Features

- Unified installed-package inventory
- Stable update detection (APT uses a read-only simulated upgrade set, so phased/kept-back upgrades do not get an Update button)
- Installed and latest version columns
- Hide system packages filter
- Package details drawer with manager, source, version, size, architecture, and description when available
- Per-package progress bar while updating
- Update All operation with a single background operation
- Recent successful update history stored in SQLite
- Explicit APT package-index refresh instead of refreshing on every page load
- Maintenance operations for APT autoremove and unused Flatpak runtimes

## Run

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
./run.sh
```

Open `http://127.0.0.1:8080`.

## Privileges

The web app is designed to run as the regular desktop user. Privileged APT/Snap operations are executed through `sudo` only when the user explicitly requests an operation. Normal page loads and update checks do not invoke `sudo`.

For a production deployment, replace the simple sudo boundary with a dedicated least-privilege polkit/systemd helper and bind the service to localhost unless authenticated remote administration is intentionally added.

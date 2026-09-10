# PackageHub — Linux Package Manager

Local web application for managing and discovering software across Linux package managers from one interface.

## Supported package managers

PackageHub detects and uses whichever supported managers are installed on the host:

- APT / dpkg
- Snap
- Flatpak
- DNF
- Pacman
- Zypper

The manager abstraction is designed so additional package managers can be added without changing the frontend.

## Features

- Unified installed-package inventory
- Stable update detection
- Installed and latest version columns
- Hide system packages filter
- Package details drawer with manager, source, version, size, architecture and description when available
- Per-package progress bar while updating
- Recent successful package-operation history stored in SQLite
- Explicit package-index refresh instead of privileged work during page loads
- APT autoremove and unused Flatpak-runtime cleanup
- **Find software** search across every supported package manager detected on the host
- Search results grouped by package manager/source
- Search results show source, latest version and installed/update state
- Search-result details support install, update and remove actions depending on package state
- Flatpak search results retain the remote/source so an install can target that source
- No package-manager shell commands are accepted directly from the UI
- Package and search inputs are validated before command execution

## Search workflow

Open **Find software**, enter a package/application name, and PackageHub queries each available package manager. Results are grouped by source, for example:

```text
APT
  APT repositories

Snap
  Snap Store

Flatpak
  flathub
```

Clicking a result opens its details. Depending on the package state, the drawer presents:

- Install — package is available but not installed
- Update — a stable update is available for an installed package
- Remove — package is installed

## Privileges

The web app runs as the regular desktop user. It does **not** store sudo passwords. For privileged package operations, it first checks whether the current user's sudo credential timestamp is already valid. When it is not, the UI asks for the password once and the backend uses `sudo -S -v` to prime the normal sudo credential cache. Subsequent commands use `sudo -n` and do not receive the password.

Normal page loads, package searches, package details, and stable-update checks do not request administrator privileges.

For a production multi-user deployment, replace this local sudo boundary with a dedicated least-privilege polkit/systemd helper and keep the web service bound to localhost unless authenticated remote administration is intentionally enabled.

## Run

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
./run.sh
```

Open:

`http://127.0.0.1:8080`

## Tests

```bash
pytest -q
```

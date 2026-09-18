#!/bin/sh
set -eu
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

if command -v python3 >/dev/null 2>&1 && python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'; then
  exec python3 "$SCRIPT_DIR/install.py"
fi

if ! command -v pkexec >/dev/null 2>&1; then
  printf '%s\n' 'Python 3.11+ fehlt und pkexec ist für die grafische Systemabfrage nicht verfügbar.' >&2
  exit 1
fi

if command -v apt-get >/dev/null 2>&1; then
  pkexec apt-get install -y python3 python3-venv
elif command -v dnf >/dev/null 2>&1; then
  pkexec dnf install -y python3
elif command -v pacman >/dev/null 2>&1; then
  pkexec pacman -S --needed --noconfirm python
else
  printf '%s\n' 'Keine unterstützte Paketverwaltung für den Python-Bootstrap gefunden.' >&2
  exit 1
fi

if ! command -v python3 >/dev/null 2>&1 || ! python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'; then
  printf '%s\n' 'Die Paketverwaltung konnte Python 3.11 oder neuer nicht bereitstellen.' >&2
  exit 1
fi
exec python3 "$SCRIPT_DIR/install.py"

#!/usr/bin/env bash
# Installation : environnement virtuel, dépendances, base SQLite.
set -euo pipefail
cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3}"
if ! "$PYTHON" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
  echo "Python 3.10 ou plus est requis (essayé : $PYTHON). Indiquer un autre interpréteur : PYTHON=python3.12 ./setup.sh" >&2
  exit 1
fi

[ -d .venv ] || "$PYTHON" -m venv .venv
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt
.venv/bin/python manage.py migrate

echo
echo "Installation terminée. Lancer l'application : ./run.sh"

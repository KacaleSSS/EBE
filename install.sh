#!/bin/sh
set -eu
ebe_root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
python3 -m venv "$ebe_root/.venv"
"$ebe_root/.venv/bin/python" -m pip install "$ebe_root[pdf,images]"
"$ebe_root/.venv/bin/ebe" doctor
printf '%s\n' 'Ready. Run .venv/bin/ebe --help'

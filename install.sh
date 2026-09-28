#!/bin/sh
set -eu
ebe_root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ebe_python=${EBE_PYTHON:-python3}
if ! "$ebe_python" -c 'import sys; assert sys.version_info >= (3, 11)' 2>/dev/null; then
    if [ -n "${EBE_PYTHON:-}" ]; then
        printf '%s\n' 'EBE_PYTHON must point to Python 3.11+' >&2
        exit 1
    fi
    ebe_python=''
    for ebe_candidate in python3.14 python3.13 python3.12 python3.11; do
        if command -v "$ebe_candidate" >/dev/null 2>&1 && "$ebe_candidate" -c 'import sys; assert sys.version_info >= (3, 11)' 2>/dev/null; then
            ebe_python=$ebe_candidate
            break
        fi
    done
    [ -n "$ebe_python" ] || { printf '%s\n' 'Install Python 3.11+ first.' >&2; exit 1; }
fi
if [ -d "$ebe_root/.venv" ]; then
    "$ebe_root/.venv/bin/python" -c 'import sys; assert sys.version_info >= (3, 11)' || {
        printf '%s\n' 'Existing .venv is incompatible. Use a fresh extracted directory; no files were deleted.' >&2
        exit 1
    }
fi
"$ebe_python" -m venv "$ebe_root/.venv"
"$ebe_root/.venv/bin/python" -m pip install --upgrade 'pip>=23'
"$ebe_root/.venv/bin/python" -m pip install "$ebe_root[pdf,images]"
"$ebe_root/.venv/bin/ebe" doctor
printf '%s\n' 'Ready. Run .venv/bin/ebe --help'

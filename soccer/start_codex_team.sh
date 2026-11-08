#!/bin/sh
set -eu
REPO=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec python3 "$REPO/scripts/codex_team/team.py" "$@" start

#!/bin/bash
# Wrapper for scheduled (launchd) runs. launchd jobs don't inherit your shell
# profile, so secrets live in .env (gitignored) instead of relying on
# ~/.zshrc/.zprofile exports.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

if [ -f .env ]; then
  set -a
  source .env
  set +a
fi

exec python3 job_hunt.py "$@"

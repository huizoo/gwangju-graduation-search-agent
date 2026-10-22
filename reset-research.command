#!/bin/zsh
set -eu
APP_ROOT="$(cd -- "$(dirname -- "$0")" && pwd)"
cd "$APP_ROOT"
if [[ ! -x ".venv/bin/python" ]]; then
  print "먼저 README의 Python 가상환경 설치를 진행해 주세요."
  if [[ -t 0 ]]; then
    read -r "?이 창을 닫으려면 Enter를 누르세요." || true
  fi
  exit 1
fi
RESET_EXIT_CODE=0
if .venv/bin/python scripts/reset_research_data.py --interactive; then
  RESET_EXIT_CODE=0
else
  RESET_EXIT_CODE=$?
fi
print ""
if [[ -t 0 ]]; then
  read -r "?이 창을 닫으려면 Enter를 누르세요." || true
fi
exit "$RESET_EXIT_CODE"

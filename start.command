#!/bin/zsh
set -eu
APP_ROOT="$(cd -- "$(dirname -- "$0")" && pwd)"
cd "$APP_ROOT"
if [[ ! -x ".venv/bin/python" ]]; then
  print "먼저 README의 Python 가상환경 설치를 진행해 주세요."
  exit 1
fi
exec .venv/bin/python -m app

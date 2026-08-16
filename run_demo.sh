#!/usr/bin/env bash
# One-shot demo: bring the three services up, run scenarios, tear down.
#   ./run_demo.sh                 all scenarios
#   ./run_demo.sh transient       one scenario
#   ./run_demo.sh transient --compare   policy agent vs naive baseline
set -uo pipefail
cd "$(dirname "$0")"

bash start_services.sh || exit 1
trap 'bash stop_services.sh >/dev/null 2>&1' EXIT

PY=${PYTHON:-python3}
if [ $# -eq 0 ]; then
  $PY run_agent.py --all
else
  SCN=$1; shift
  $PY run_agent.py --scenario "$SCN" "$@"
fi

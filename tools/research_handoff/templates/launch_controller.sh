#!/bin/sh
set -eu
controller_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
controller_config=${1:?config path required}
controller_run=${2:?run id required}
controller_state=${3:?absolute state directory required}
controller_python=${PYTHON:-python3}
"$controller_python" -B "$controller_root/controller.py" --state-dir "$controller_state" init --config "$controller_config" --run-id "$controller_run"
exec "$controller_python" -B "$controller_root/controller.py" --state-dir "$controller_state" start --run-id "$controller_run" --background

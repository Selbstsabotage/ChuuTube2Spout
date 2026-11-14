#!/bin/bash
# Start ChuuTubePoi from source (macOS).
#
#     bash run.sh                  the app
#     bash run.sh --doctor         check the setup
#     bash run.sh --check "URL"    find out why a link will not play
#
# Run setup.sh once first.

cd "$(dirname "$0")" || exit 1
if [ ! -x .venv/bin/python ]; then
    echo "Run the setup first:  bash setup.sh"
    exit 1
fi
exec .venv/bin/python run.py "$@"

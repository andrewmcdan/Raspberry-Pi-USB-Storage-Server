#!/usr/bin/env bash
# Run from an extracted release or checkout on an existing Pi USB Publisher.
set -Eeuo pipefail
if [[ ${EUID} -ne 0 ]]; then
    echo "Run this upgrade as root: sudo bash ./upgrade-gcode-viewer.sh" >&2
    exit 1
fi
if (( $# != 0 )); then
    echo "Usage: sudo bash ./upgrade-gcode-viewer.sh" >&2
    exit 1
fi
SOURCE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec /usr/bin/python3 "${SOURCE_DIR}/scripts/upgrade_gcode_viewer.py"

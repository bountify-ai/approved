#!/usr/bin/env bash
# tryit-smoke: build the try-it image and drive it headlessly through its public port
# (images/tryit/smoke.py has the assertions). SKIP_BUILD=1 uses an image already built;
# KEEP=1 leaves the last container running. Removes only its own container and volume.
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
image="${TRYIT_IMAGE:-approved-tryit:local}"
if [ "${SKIP_BUILD:-0}" != "1" ]; then
  docker build -q -f "$root/images/tryit/Dockerfile" -t "$image" "$root" >/dev/null
fi
TRYIT_IMAGE="$image" exec python3 "$root/images/tryit/smoke.py"

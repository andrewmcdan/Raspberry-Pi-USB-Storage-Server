#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
docker build -f manager/Dockerfile -t piusb-manager:local .
if [[ ! -f .env ]]; then
    docker run --rm -it --user "$(id -u):$(id -g)" --mount "type=bind,source=$PWD,target=/work" --workdir /work piusb-manager:local python -m manager.configure --url "${1:-https://piusb.example.internal}"
    chmod 600 .env
fi
docker compose up -d
echo 'Manager is listening on the configured host address and port (default 0.0.0.0:8881). Configure HTTPS using docs/manager.md before connecting Pis.'

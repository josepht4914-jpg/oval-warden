#!/bin/sh
set -eu
cd "$(dirname "$0")"

image=oval-warden:check
docker build --platform linux/amd64 --tag "$image" .
exec python3 kit/check/check.py --map-dir /arena -- \
    docker run --rm -i --platform linux/amd64 \
    --network none --cpus 1 --memory 512m --memory-swap 512m \
    --read-only --tmpfs /tmp:rw,size=64m --pids-limit 128 \
    --cap-drop ALL --user 65534:65534 \
    --volume "$PWD/kit/arena:/arena:ro" \
    "$image"

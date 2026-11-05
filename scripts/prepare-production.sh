#!/bin/sh
set -eu

# Run on the Linux Docker host before deploying the production stack.
# For a custom TRACCAR_VOLUME_ROOT, pass that same path as the first argument.
volume_root="${1:-/home/zeeshan/containers/volumes/traccar}"

if [ "$#" -gt 1 ]; then
    echo "Usage: sudo $0 [TRACCAR_VOLUME_ROOT]" >&2
    exit 1
fi
case "$volume_root" in
    /*) ;;
    *) echo "The volume root must be an absolute path." >&2; exit 1 ;;
esac
if [ "$(id -u)" -ne 0 ]; then
    echo "Run this script with sudo on the Docker host to prepare the host folders." >&2
    exit 1
fi

# Do not change existing database ownership or modes. Its image initializes these.
umask 022
mkdir -p -- "$volume_root/database" "$volume_root/logs" "$volume_root/data"

echo "Prepared production folders under $volume_root"
echo "Adapter state uses a Docker-managed named volume; no host folder is needed."

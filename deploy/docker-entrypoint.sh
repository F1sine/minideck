#!/bin/sh
# Starts the Minideck server with settings from environment variables.
# Extra arguments (e.g. --reset-admin) are passed through.
set -e
set -- --port "${MINIDECK_PORT:-7072}" --data "${MINIDECK_DATA:-/data}" "$@"
if [ -n "$MINIDECK_PUBLIC" ]; then
    set -- --public "$MINIDECK_PUBLIC" "$@"
fi
exec python /app/run_server.py "$@"

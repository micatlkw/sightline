#!/usr/bin/env bash
set -e

PUID=${PUID:-1026}
PGID=${PGID:-100}

echo "[entrypoint] Initializing Sightline container (PUID=${PUID}, PGID=${PGID})..."

# If running as root, configure user/group and drop privileges
if [ "$(id -u)" = "0" ]; then
    # Create group if it does not already exist
    if ! getent group "$PGID" >/dev/null 2>&1; then
        groupadd -g "$PGID" sightline 2>/dev/null || groupadd sightline 2>/dev/null || true
    fi

    # Create user if it does not already exist
    if ! getent passwd "$PUID" >/dev/null 2>&1; then
        useradd -u "$PUID" -g "$PGID" -d /home/sightline -m -s /bin/bash sightline 2>/dev/null || true
    fi

    # Set up HOME directory for non-root user (prevents write errors to /root/.config or /root/.cache)
    export HOME=/home/sightline
    mkdir -p /home/sightline /data /models/.config /models/torch /config

    # Ensure runtime directories are owned and readable/writable by target user
    chown -R "$PUID:$PGID" /home/sightline /data /models /config 2>/dev/null || true
    chmod -R u+rwX,g+rwX /home/sightline /data /models /config 2>/dev/null || true

    echo "[entrypoint] Permissions configured. Dropping privileges to ${PUID}:${PGID}..."
    if command -v gosu >/dev/null 2>&1; then
        exec gosu "$PUID:$PGID" "$@"
    fi
fi

# Fallback or already non-root
exec "$@"

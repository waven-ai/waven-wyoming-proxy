#!/bin/sh
# Drop to the unprivileged `waven` user before running the proxy.
#
# The container has to START as root: /data is a volume the Supervisor (or
# `docker run -v`) mounts at RUNTIME, so a build-time chown can't reach it, and
# the daily-cap state file lives there. Fix ownership here, then hand the
# process to gosu, which exec's — no extra PID, signals and exit codes pass
# straight through, which is what `init: false` in config.yaml relies on.
#
# If the image is already being run as a non-root user (`docker run --user`),
# skip straight to the command.
set -e

if [ "$(id -u)" = "0" ]; then
    # Best-effort: a read-only or oddly-owned /data must not stop voice working
    # — DailyCap.save() already degrades to "no persistence" on OSError.
    chown -R waven:waven /data 2>/dev/null || true
    exec gosu waven "$@"
fi

exec "$@"

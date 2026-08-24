#!/bin/sh
# Optional Docker HEALTHCHECK wrapper. Invoke with `sh` (see README, "Expose
# the admin page") so no executable bit is needed on this file.
#
# strfry only spawns plugin86.py (and, through it, server86.py) lazily, on
# the relay's first write after container start — confirmed against a real
# container: plugin86.py never runs until an event is actually published,
# not merely because strfry is up and healthy. A quiet relay's admin page
# can therefore stay down indefinitely after every `docker restart` or host
# reboot. Docker's own HEALTHCHECK exec is the one thing guaranteed to run
# on a fixed interval regardless of relay traffic, so this rides on it as a
# side effect — never as the health signal itself.
#
# The exit code below is ALWAYS whatever the wrapped command reports, never
# this script's own doing. That must stay true in both directions: this can
# never fake "healthy" on its own account (a lie that would hide a real
# relay outage), and a hiccup in this admin sidecar can never report
# "unhealthy" and get the relay itself depooled by an orchestrator over a
# problem that isn't the relay's.
#
# Usage — wrap whatever CMD your image's own HEALTHCHECK already runs (or
# `strfry scan --count {}` if it defines none):
#   healthcheck:
#     test: ["CMD", "sh", "/config/strfry86/ensure-server86.sh",
#            "/app/strfry", "--config=/etc/strfry.conf", "scan", "--count", "{}"]
#     interval: 2s
#     timeout: 5s
#     retries: 30

python3 /config/strfry86/plugin86.py --ensure-server86 >/dev/null 2>&1
exec "$@"

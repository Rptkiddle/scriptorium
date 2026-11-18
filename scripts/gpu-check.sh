#!/bin/sh
# Restarts the web service and the worker when their containers have lost
# the GPU, so they embed on it again. On some machines running containers
# lose the GPU now and then (for instance when systemd reloads its units);
# Scriptorium then carries on on the CPU, far slower, until a restart.
#
# Nothing is done when the machine itself has no working GPU, when the
# containers still have theirs, or while a job is running (the next call
# tries again). Run it from the crontab of an account that may run Docker:
#   10 3,5 * * * /path/to/scriptorium/scripts/gpu-check.sh >> /srv/scriptorium/data/logs/gpu-check.log 2>&1
# DOCKER names the Docker command, e.g. DOCKER="sudo docker".

cd "$(dirname "$0")/.." || exit 1
DOCKER=${DOCKER:-docker}
now() { date "+%Y-%m-%d %H:%M"; }
has_gpu() {
    $DOCKER compose exec -T "$1" python -c \
        "import sys, torch; sys.exit(0 if torch.cuda.is_available() else 1)" >/dev/null 2>&1
}

nvidia-smi -L >/dev/null 2>&1 || { echo "$(now) the machine's GPU does not answer; nothing done"; exit 0; }
lost=""
for service in server worker; do
    has_gpu "$service" || lost="$lost $service"
done
[ -z "$lost" ] && exit 0
running=$($DOCKER compose exec -T db psql -U scriptorium -d scriptorium -tAc \
    "SELECT count(*) FROM jobs WHERE status = 'running'" 2>/dev/null)
if [ "$running" != "0" ]; then
    echo "$(now) no GPU in:$lost; a job is running (or the database did not answer), so no restart"
    exit 0
fi
echo "$(now) no GPU in:$lost; restarting"
# shellcheck disable=SC2086
$DOCKER compose restart $lost

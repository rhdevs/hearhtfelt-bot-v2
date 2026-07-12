#!/usr/bin/env bash
#
# Remote deploy script — runs ON the droplet, fed via `ssh ... bash -s < deploy/deploy.sh`.
# Swaps the running heartfelt-bot container for a freshly pulled image, with a
# health check and automatic rollback to the previous container on failure.
#
# Required env (exported by the ssh command line from GitHub Actions):
#   GHCR_TOKEN  - token with read access to the GHCR package
#   GHCR_USER   - username for `docker login ghcr.io`
#   IMAGE       - full image ref to run, e.g. ghcr.io/rhdevs/heartfelt-bot:latest
set -euo pipefail

: "${GHCR_TOKEN:?GHCR_TOKEN is required}"
: "${GHCR_USER:?GHCR_USER is required}"
: "${IMAGE:?IMAGE is required}"

CONTAINER=heartfelt-bot
OLD="${CONTAINER}-old"
ENV_FILE=/root/heartfelt-bot/.env

if [ ! -f "$ENV_FILE" ]; then
  echo "ERROR: env file $ENV_FILE not found on droplet" >&2
  exit 1
fi

echo "==> Logging in to GHCR and pulling $IMAGE"
echo "$GHCR_TOKEN" | docker login ghcr.io -u "$GHCR_USER" --password-stdin
docker pull "$IMAGE"

echo "==> Rotating current container aside for rollback"
docker rm -f "$OLD" 2>/dev/null || true
if docker ps -a --format '{{.Names}}' | grep -qx "$CONTAINER"; then
  docker rename "$CONTAINER" "$OLD"
  docker stop "$OLD" >/dev/null 2>&1 || true
fi

echo "==> Starting new container"
docker run -d \
  --name "$CONTAINER" \
  --restart unless-stopped \
  --env-file "$ENV_FILE" \
  "$IMAGE"

echo "==> Health check (giving it a few seconds to settle)"
sleep 8
RUNNING="$(docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null || echo false)"

if [ "$RUNNING" = "true" ]; then
  echo "==> Deploy OK — new container is running"
  docker logs --tail 15 "$CONTAINER" 2>&1 || true
  docker rm -f "$OLD" 2>/dev/null || true
  docker image prune -f >/dev/null 2>&1 || true
  docker logout ghcr.io >/dev/null 2>&1 || true
  exit 0
fi

echo "==> Deploy FAILED — new container is not running. Recent logs:" >&2
docker logs --tail 50 "$CONTAINER" 2>&1 || true
docker rm -f "$CONTAINER" 2>/dev/null || true
if docker ps -a --format '{{.Names}}' | grep -qx "$OLD"; then
  echo "==> Rolling back to previous container" >&2
  docker rename "$OLD" "$CONTAINER"
  docker start "$CONTAINER" >/dev/null 2>&1 || true
fi
docker logout ghcr.io >/dev/null 2>&1 || true
exit 1

#!/bin/bash
# Keep docker disk in check during the run (45G free, 104 images). Every 120s:
# drop dangling images/build cache/stopped containers. Never touches running ones.
while true; do
  docker container prune -f >/dev/null 2>&1
  docker image prune -f >/dev/null 2>&1
  docker builder prune -f >/dev/null 2>&1
  # If free space under 6G, also drop unused (non-dangling) images to avoid a stall.
  avail=$(df --output=avail -BG / | tail -1 | tr -dc '0-9')
  if [ "${avail:-99}" -lt 6 ]; then docker image prune -af >/dev/null 2>&1; fi
  sleep 120
done

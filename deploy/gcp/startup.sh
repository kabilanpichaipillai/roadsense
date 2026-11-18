#!/bin/sh
# VM startup script: Docker and Compose from Ubuntu's own packages, once.
if ! command -v docker >/dev/null 2>&1; then
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y docker.io docker-compose-v2
  systemctl enable --now docker
fi

#!/bin/sh
# Update a running RoadSense VM from a code bundle (used by .github/workflows/cd.yml).
# Keeps what the first deploy (scripts/deploy_gcp.py) put there and the code does not carry: .env (secrets),
# weights/ and the Docker volumes (reports, photos, certificates).
set -eu
BUNDLE=${1:?usage: update.sh <bundle.tgz>}
cd /opt/roadsense
if [ ! -f .env ] || [ ! -d weights ]; then
  echo "No deployment here yet: run scripts/deploy_gcp.py once first" >&2
  exit 1
fi
sudo rm -rf roadsense scripts demo
sudo tar -xzf "$BUNDLE" -C /opt/roadsense
rm -f "$BUNDLE"
sudo docker compose up -d --build --remove-orphans
sudo docker image prune -f >/dev/null
echo "Running revision $(cat REVISION)"

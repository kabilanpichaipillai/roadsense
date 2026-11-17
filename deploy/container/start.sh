#!/bin/sh
# On an empty data volume, seed the keyless demo trips first; later starts keep whatever is there.
set -e
if [ ! -f /app/data/demo/roadsense.db ]; then
  echo "Seeding the demo trips (keyless replay, no LLM calls)..."
  python scripts/demo.py || python scripts/demo.py --no-llm
fi
exec python scripts/run_all.py --host 0.0.0.0

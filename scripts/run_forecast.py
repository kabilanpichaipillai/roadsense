#!/usr/bin/env python
"""Freeze-thaw pothole risk forecast from the command line (the agent API also runs it on a schedule).

    python scripts/run_forecast.py                       # forecast now (skips itself if nothing changed)
    python scripts/run_forecast.py --force               # recompute even if the inputs are unchanged
    python scripts/run_forecast.py --evaluate            # score earlier forecasts against later sightings
    python scripts/run_forecast.py --backtest 2026-03-01 2026-03-15 2026-04-01
                                                         # forecast as of past dates with only what was known then

The score is a configurable heuristic (roadsense/core/forecast_rules.json), not a trained model.
"""
import argparse
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from roadsense.common.db import session  # noqa: E402
from roadsense.core import forecast  # noqa: E402


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--force", action="store_true", help="recompute even when detections and weather are unchanged")
    p.add_argument("--horizon", type=int, help="forecast horizon in days (default ROADSENSE_FORECAST_HORIZON_DAYS)")
    p.add_argument("--evaluate", action="store_true", help="only record outcomes of earlier forecasts")
    p.add_argument("--backtest", nargs="+", type=date.fromisoformat, metavar="YYYY-MM-DD",
                   help="re-run the forecast as of these past dates and evaluate it")
    a = p.parse_args()
    with session() as conn:
        if a.backtest:
            out = forecast.backtest(conn, a.backtest)
        elif a.evaluate:
            out = {"outcomes": forecast.evaluate_outcomes(conn), "evaluation": forecast.evaluation_summary(conn)}
        else:
            out = forecast.run_forecast(conn, force=a.force, actor="cli", horizon_days=a.horizon)
            out["outcomes"] = forecast.evaluate_outcomes(conn)
    print(json.dumps(out, indent=2, default=str))
    print(f"\nNote: {forecast.DISCLAIMER}")


if __name__ == "__main__":
    main()

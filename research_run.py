#!/usr/bin/env python3
"""
research_run.py — autoresearch runner for polymarket-weather-trader

Runs a single weather scan + optional trade execution with configurable
overrides. Reports a structured result summary to stdout.

Usage:
  python3 research_run.py                  # dry-run, default config
  python3 research_run.py --live           # execute real trades (sim venue)
  python3 research_run.py --cycles N       # run N passes (default: 1)

Config overrides via env vars:
  RESEARCH_ENTRY=0.20            # entry threshold (default: 0.15)
  RESEARCH_EXIT=0.55             # exit threshold (default: 0.45)
  RESEARCH_MAX_BET=5.0           # max $ per trade (default: 2.00)
  RESEARCH_MAX_TRADES=5          # max trades per run (default: 5)
  RESEARCH_LOCATIONS=NYC,Chicago # locations to scan
  RESEARCH_BINARY_ONLY=true      # skip range-bucket events
  RESEARCH_NO_SAFEGUARDS=true    # disable safeguards
  RESEARCH_SLIPPAGE_MAX=0.30     # slippage tolerance
  RESEARCH_MIN_LIQUIDITY=0       # min liquidity filter
"""

import os
import sys
import json
import time
import argparse
from datetime import datetime, timezone
from pathlib import Path

# ── Env var overrides (must happen BEFORE weather_trader is imported) ────────
_OVERRIDES = {
    "RESEARCH_ENTRY":         "SIMMER_WEATHER_ENTRY_THRESHOLD",
    "RESEARCH_EXIT":          "SIMMER_WEATHER_EXIT_THRESHOLD",
    "RESEARCH_MAX_BET":       "SIMMER_WEATHER_MAX_POSITION_USD",
    "RESEARCH_MAX_TRADES":    "SIMMER_WEATHER_MAX_TRADES_PER_RUN",
    "RESEARCH_LOCATIONS":     "SIMMER_WEATHER_LOCATIONS",
    "RESEARCH_BINARY_ONLY":   "SIMMER_WEATHER_BINARY_ONLY",
    "RESEARCH_SLIPPAGE_MAX":  "SIMMER_WEATHER_SLIPPAGE_MAX",
    "RESEARCH_MIN_LIQUIDITY": "SIMMER_WEATHER_MIN_LIQUIDITY",
}

for research_key, sdk_key in _OVERRIDES.items():
    if research_key in os.environ:
        os.environ[sdk_key] = os.environ[research_key]
        print(f"[research] {sdk_key}={os.environ[sdk_key]}", flush=True)

# Force sim venue for research unless overridden
if "TRADING_VENUE" not in os.environ:
    os.environ["TRADING_VENUE"] = "sim"
    print(f"[research] TRADING_VENUE=sim", flush=True)

# ── Now import weather_trader ────────────────────────────────────────────────
import weather_trader as _wt

# ── Direct module patches (belt-and-suspenders vs automaton remote config) ───
if "RESEARCH_ENTRY" in os.environ:
    _wt.ENTRY_THRESHOLD = float(os.environ["RESEARCH_ENTRY"])
    print(f"[research] ENTRY_THRESHOLD patched → {_wt.ENTRY_THRESHOLD}", flush=True)
if "RESEARCH_EXIT" in os.environ:
    _wt.EXIT_THRESHOLD = float(os.environ["RESEARCH_EXIT"])
    print(f"[research] EXIT_THRESHOLD patched → {_wt.EXIT_THRESHOLD}", flush=True)
if "RESEARCH_MAX_BET" in os.environ:
    _wt.MAX_POSITION_USD = float(os.environ["RESEARCH_MAX_BET"])
    print(f"[research] MAX_POSITION_USD patched → {_wt.MAX_POSITION_USD}", flush=True)
if "RESEARCH_MAX_TRADES" in os.environ:
    _wt.MAX_TRADES_PER_RUN = int(os.environ["RESEARCH_MAX_TRADES"])
    print(f"[research] MAX_TRADES_PER_RUN patched → {_wt.MAX_TRADES_PER_RUN}", flush=True)
if "RESEARCH_LOCATIONS" in os.environ:
    locs = [l.strip() for l in os.environ["RESEARCH_LOCATIONS"].split(",") if l.strip()]
    _wt.ACTIVE_LOCATIONS = locs
    print(f"[research] ACTIVE_LOCATIONS patched → {_wt.ACTIVE_LOCATIONS}", flush=True)
if "RESEARCH_BINARY_ONLY" in os.environ:
    _wt.BINARY_ONLY = os.environ["RESEARCH_BINARY_ONLY"].lower() in ("true", "1")
    print(f"[research] BINARY_ONLY patched → {_wt.BINARY_ONLY}", flush=True)
if "RESEARCH_SLIPPAGE_MAX" in os.environ:
    _wt.SLIPPAGE_MAX_PCT = float(os.environ["RESEARCH_SLIPPAGE_MAX"])
    print(f"[research] SLIPPAGE_MAX_PCT patched → {_wt.SLIPPAGE_MAX_PCT}", flush=True)

# ── Argument parsing ─────────────────────────────────────────────────────────
parser = argparse.ArgumentParser()
parser.add_argument("--live", action="store_true", help="Execute real trades")
parser.add_argument("--cycles", type=int, default=1, help="Number of scan cycles")
args = parser.parse_args()

dry_run = not args.live
use_safeguards = os.environ.get("RESEARCH_NO_SAFEGUARDS", "").lower() not in ("true", "1")

# ── Balance snapshot ─────────────────────────────────────────────────────────
def get_sim_balance():
    try:
        data = _wt.get_client(live=False)._request("GET", "/api/sdk/portfolio")
        return float(data.get("sim_balance") or 0)
    except Exception:
        return None

def get_unrealized_pnl():
    try:
        data = _wt.get_client(live=False)._request("GET", "/api/sdk/portfolio")
        return float(data.get("sim_pnl") or 0)
    except Exception:
        return None

print(f"\n[research] Starting weather-trader research run", flush=True)
print(f"[research] venue=sim | cycles={args.cycles} | live={args.live}", flush=True)
print(f"[research] entry={_wt.ENTRY_THRESHOLD:.2f} | exit={_wt.EXIT_THRESHOLD:.2f} | max_bet={_wt.MAX_POSITION_USD:.2f}", flush=True)
print(f"[research] locations={','.join(_wt.ACTIVE_LOCATIONS)}", flush=True)
print(f"[research] safeguards={'on' if use_safeguards else 'off'} | binary_only={_wt.BINARY_ONLY}", flush=True)

balance_start = get_sim_balance()
print(f"\n[research] Balance start: {balance_start} $SIM", flush=True)

# ── Scan loop ────────────────────────────────────────────────────────────────
total_trades = 0
total_opportunities = 0
total_events_scanned = 0
total_usd_spent = 0.0
cycle_results = []

for cycle in range(args.cycles):
    if args.cycles > 1:
        print(f"\n[research] === Cycle {cycle+1}/{args.cycles} ===", flush=True)

    # Capture stdout to parse summary
    import io
    from contextlib import redirect_stdout

    f = io.StringIO()
    try:
        with redirect_stdout(f):
            _wt.run_weather_strategy(
                dry_run=dry_run,
                use_safeguards=use_safeguards,
                use_trends=True,
                smart_sizing=False,
                quiet=False,
            )
    except SystemExit:
        pass

    output = f.getvalue()
    print(output, flush=True)

    # Parse summary from output
    trades_cycle = 0
    opps_cycle = 0
    events_cycle = 0
    spent_cycle = 0.0

    for line in output.splitlines():
        if "Trades executed:" in line:
            try: trades_cycle = int(line.split(":")[-1].strip())
            except: pass
        if "Entry opportunities:" in line:
            try: opps_cycle = int(line.split(":")[-1].strip())
            except: pass
        if "Events scanned:" in line:
            try: events_cycle = int(line.split(":")[-1].strip())
            except: pass

    total_trades += trades_cycle
    total_opportunities += opps_cycle
    total_events_scanned += events_cycle

    cycle_results.append({
        "cycle": cycle + 1,
        "trades": trades_cycle,
        "opportunities": opps_cycle,
        "events": events_cycle,
    })

    if args.cycles > 1 and cycle < args.cycles - 1:
        time.sleep(5)

# ── Final snapshot ───────────────────────────────────────────────────────────
balance_end = get_sim_balance()
balance_delta = (balance_end - balance_start) if (balance_end and balance_start) else None
unrealized = get_unrealized_pnl()

print("\n" + "=" * 60, flush=True)
print("[research] SUMMARY", flush=True)
print("=" * 60, flush=True)
print(f"  Cycles:          {args.cycles}", flush=True)
print(f"  Events scanned:  {total_events_scanned}", flush=True)
print(f"  Opportunities:   {total_opportunities}", flush=True)
print(f"  Trades executed: {total_trades}", flush=True)
if balance_delta is not None:
    print(f"  Balance delta:   {balance_delta:+.2f} $SIM", flush=True)
if unrealized is not None:
    print(f"  Unrealized PnL:  {unrealized:+.2f} $SIM", flush=True)

summary = {
    "research_summary": True,
    "cycles": args.cycles,
    "events_scanned": total_events_scanned,
    "opportunities": total_opportunities,
    "trades_executed": total_trades,
    "balance_start": balance_start,
    "balance_end": balance_end,
    "balance_delta": balance_delta,
    "unrealized_pnl": unrealized,
}
print(json.dumps(summary), flush=True)

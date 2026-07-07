"""polyarb CLI.

    python -m polyarb scan                  # one-shot detection, print results
    python -m polyarb monitor               # continuous paper monitoring
    python -m polyarb run --paper           # monitor + simulated executions
    python -m polyarb run --live            # REAL MONEY (triple env gate)
    python -m polyarb report                # summarize the ledger
"""

from __future__ import annotations

import argparse
import logging
import sys

from .detector import DetectorConfig
from .execution import PaperExecutor
from .ledger import Ledger
from .risk import RiskConfig, RiskManager
from .scanner import Scanner, ScannerConfig


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="polyarb", description="Polymarket negRisk arbitrage bot"
    )
    p.add_argument("--data-dir", default="polyarb_data", help="ledger directory")
    p.add_argument("--config", default=None,
                   help="TradingConfig JSON file (overrides scan-arg defaults; "
                        "hot-reloaded by the --ws daemon at universe refresh)")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_scan_args(sp):
        sp.add_argument("--max-events", type=int, default=500)
        sp.add_argument("--min-liquidity", type=float, default=0.0)
        sp.add_argument("--min-edge", type=float, default=0.01,
                        help="min profit per basket-share after fees ($)")
        sp.add_argument("--min-profit", type=float, default=0.50,
                        help="min absolute profit per opportunity ($)")
        sp.add_argument("--max-notional", type=float, default=250.0,
                        help="max capital per opportunity ($)")
        sp.add_argument("--max-legs", type=int, default=30)
        sp.add_argument("--prefilter-slack", type=float, default=0.03)
        sp.add_argument("--allow-augmented", action="store_true",
                        help="allow long-YES on augmented events w/o Other")

    sp_scan = sub.add_parser("scan", help="one-shot scan")
    add_scan_args(sp_scan)

    sp_mon = sub.add_parser("monitor", help="continuous detection (no orders)")
    add_scan_args(sp_mon)
    sp_mon.add_argument("--interval", type=float, default=5.0)
    sp_mon.add_argument("--duration", type=float, default=None,
                        help="seconds to run (default: forever)")
    sp_mon.add_argument("--ws", action="store_true",
                        help="event-driven WebSocket books (low latency)")

    sp_run = sub.add_parser("run", help="monitor + execute")
    add_scan_args(sp_run)
    sp_run.add_argument("--interval", type=float, default=5.0)
    sp_run.add_argument("--duration", type=float, default=None)
    sp_run.add_argument("--ws", action="store_true",
                        help="event-driven WebSocket books (low latency)")
    mode = sp_run.add_mutually_exclusive_group(required=True)
    mode.add_argument("--paper", action="store_true", help="simulated fills")
    mode.add_argument("--live", action="store_true",
                      help="REAL orders (requires triple env gate + wallet env)")
    sp_run.add_argument("--max-trade", type=float, default=100.0,
                        help="risk: max $ per trade")
    sp_run.add_argument("--max-daily", type=float, default=500.0,
                        help="risk: max $ per UTC day")
    sp_run.add_argument("--cooldown", type=float, default=900.0,
                        help="risk: seconds before re-trading the same event "
                             "(your fill consumes the mispricing; paper fills "
                             "don't, so keep this high for honest paper P&L)")
    sp_run.add_argument("--fill-delay-ms", type=float, default=0.0,
                        help="paper+--ws only: re-check LIVE books this many "
                             "ms after detection and fill against what is "
                             "still there (latency-honest shadow fills)")

    sub.add_parser("report", help="summarize the opportunity/execution ledger")
    return p


def make_scanner(args, executor=None, risk=None):
    tcfg = None
    if getattr(args, "config", None):
        from .tuning import load_config

        tcfg = load_config(args.config)
    if tcfg is not None:
        det = tcfg.to_detector_cfg()
        det.allow_augmented_long_yes = args.allow_augmented
        scfg = ScannerConfig(
            max_events=tcfg.max_events,
            min_liquidity=tcfg.min_liquidity,
            interval_s=getattr(args, "interval", 5.0),
        )
        if risk is None:
            risk = RiskManager(tcfg.to_risk_cfg())
    else:
        det = DetectorConfig(
            min_edge_per_share=args.min_edge,
            min_profit_usd=args.min_profit,
            max_notional_per_arb=args.max_notional,
            max_legs=args.max_legs,
            prefilter_slack=args.prefilter_slack,
            allow_augmented_long_yes=args.allow_augmented,
        )
        scfg = ScannerConfig(
            max_events=args.max_events,
            min_liquidity=args.min_liquidity,
            interval_s=getattr(args, "interval", 5.0),
        )
    cls = Scanner
    if getattr(args, "ws", False):
        from .ws_scanner import WSScanner

        cls = WSScanner
        return cls(
            detector_cfg=det,
            scanner_cfg=scfg,
            ledger=Ledger(args.data_dir),
            risk=risk or RiskManager(),
            executor=executor,
            config_path=getattr(args, "config", None),
        )
    return cls(
        detector_cfg=det,
        scanner_cfg=scfg,
        ledger=Ledger(args.data_dir),
        risk=risk or RiskManager(),
        executor=executor,
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    if args.cmd == "report":
        print(Ledger(args.data_dir).report())
        return 0
    if args.cmd == "scan":
        scanner = make_scanner(args)
        opps = scanner.scan_once()
        if not opps:
            print("No executable arbitrage found this cycle "
                  "(that is the normal state of an efficient market).")
        for opp in opps:
            print(opp.describe())
        return 0
    if args.cmd == "monitor":
        make_scanner(args).run(duration_s=args.duration)
        return 0
    if args.cmd == "run":
        if getattr(args, "config", None):
            # --config is authoritative for risk caps (that is where the
            # evolution loop's tuned, human-approved caps live). CLI
            # --max-trade/--max-daily/--cooldown are ignored so tuned caps
            # actually apply; risk is built from the config in make_scanner.
            risk = None
        else:
            risk = RiskManager(RiskConfig(
                max_notional_per_trade=args.max_trade,
                max_daily_notional=args.max_daily,
                event_cooldown_s=args.cooldown,
            ))
        if args.live:
            from .execution import LiveExecutor  # heavy import, gated

            kill_file = risk.cfg.kill_switch_file if risk else RiskConfig().kill_switch_file
            executor = LiveExecutor(kill_switch_file=kill_file)
            print("*** LIVE TRADING ENABLED — real orders will be posted ***")
        else:
            executor = PaperExecutor()
        scanner = make_scanner(args, executor=executor, risk=risk)
        if (
            not args.live
            and getattr(args, "ws", False)
            and getattr(args, "fill_delay_ms", 0) > 0
        ):
            from .execution import DelayedPaperExecutor

            scanner.executor = DelayedPaperExecutor(
                book_provider=lambda: scanner._store,
                delay_ms=args.fill_delay_ms,
            )
        scanner.run(duration_s=args.duration)
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())

# AUTONOMY.md — the self-evolving 24/7 polyarb node

Design for running polyarb as an autonomous, self-improving agent on a
$5 VPS, integrated with this repo's evolution machinery. Grounded in
two dedicated research passes (strategy-optimization pitfalls; unattended
VPS ops) plus everything in ADVANCED.md. Read this fully before
deploying; read it twice before enabling live mode.

## The one iron rule

**The thing that evolves is never the thing that touches money.**

The daemon is dumb, fast, deterministic, and boring. The evolution loop
is offline, slow, statistical, and can only emit *proposals* for a
bounded config file. Execution code, wallet handling, and risk ceilings
are not evolvable — by construction, not by policy.

## Two-loop architecture

```
            ┌────────────────────────  $5 VPS  ───────────────────────┐
            │                                                          │
  Polymarket│  FAST LOOP (24/7, deterministic, zero tokens)            │
   WS + REST│  polyarb.service (systemd Type=notify, watchdog)         │
  ◄─────────┤    WS books → detector → risk caps → executor            │
            │    reads /var/lib/polyarb/polyarb.json (hot-reload)      │
            │    appends journals: opportunities/executions/tightness  │
            │                                                          │
            │  SLOW LOOP (nightly, offline, propose-only)              │
            │  polyarb-nightly.timer                                   │
            │    1 report   — episode-deduped P&L                      │
            │    2 evolve   — replay journals → bounded genome search  │
            │                 → walk-forward + gates → PROPOSAL        │
            │    3 notify   — ntfy digest + healthchecks dead-man      │
            │                                                          │
            └───────────────┬──────────────────────────────────────────┘
                            │ human reviews proposal (phone: ntfy)
                            ▼
        evolve_trading --apply <proposal>   (STATUS must be APPROVED)
                            │
                            ▼
        polyarb.json updated (version++, .bak kept) → daemon hot-reloads
```

## What evolves, what never evolves

| Evolvable (bounded genes) | Never evolvable |
|---|---|
| min edge / safety margin / min profit | execution code, order signing |
| prefilter slack, max legs | wallet, allowances, key handling |
| notional caps (DOWN only — baseline is the ceiling) | risk ceilings upward |
| event cooldown | live/paper mode (env triple gate) |
| universe size / liquidity floor | kill-switch behavior |

Gene bounds live in `evolution/trading/genome.py`. Detection genes
cannot drop below the journal recording floor (a config looser than the
recorder can't be honestly evaluated — the journal lacks its data).

## Anti-self-deception gates (from the optimization research)

The optimizer's fitness is offline replay of the daemon's own journals.
Known ways such loops fool themselves, and the countermeasure built in:

| Failure mode | Countermeasure |
|---|---|
| phantom opportunities that never fill live (the #1 documented killer: a real 2026 Polymarket bot made +$8.3k on true arb and −$3.2k on same-journal-edge directional legs) | fitness uses depth-true episode profits; re-clips of a persisting episode decay 0.5^k; latency-honest shadow fills (`--fill-delay-ms`) calibrate the simulator against reality |
| lottery selection (fitness dominated by 1-2 fat episodes) | `anti_lottery` constraint: candidate must beat baseline with its top-3 winning episodes removed |
| multiple-testing inflation (one night of 400 random trials statistically exhausts years of daily data) | append-only trial registry (`proposals/polyarb-config/trials.jsonl`) records every candidate ever tried; AutoMergeGate min-improvement; ROADMAP: Deflated Sharpe / PBO<0.2 gating once fill counts justify it |
| overfitting the week | walk-forward: train on all-but-last day, holdout non-regression required; `walk_forward_data` constraint fails with <2 days of data |
| censored journals (can't evaluate below recorded thresholds) | daemon records at exploration floor (0.2¢ edge, $0.05 profit) — far looser than any config it trades |
| hoarding slow capital | lockup penalty: 0.05%/day charged on hold-to-resolution basket cost |
| noise-chasing config churn | propose-only; expected organic proposal cadence is weeks, not nights; one config change in flight at a time; every applied config versioned + .bak |

Research-recommended maturity gates before trusting proposals at all:
**≥30 real fills per free parameter in-sample** and ≥6 walk-forward
folds. With 9 genes that's ~270 fills — weeks of live data. Until then
the nightly loop is *accumulating evidence*, and that is its correct
output. Rollout of an approved config follows shadow → canary → full:
run it in a second paper daemon first, then live at 25% caps, then full.

## Ops (from the VPS research — copy-pasteable in ops/polyarb/)

- **Supervision**: systemd `Type=notify` + `WatchdogSec=90`; the daemon
  pets the watchdog only while the WS feed is fresh (<120s), so the
  documented silent-freeze failure mode converts to an automatic
  restart. `Restart=always`, `MemoryHigh=512M/MemoryMax=700M`,
  full sandbox (`ProtectSystem=strict`, no capabilities); state only in
  `/var/lib/polyarb`.
- **Alerting without infrastructure**: healthchecks.io dead-man (the
  only thing that catches a dead VPS) routed to ntfy.sh urgent push;
  `OnFailure=` alert unit; nightly digest doubles as the daily P&L
  report and the nightly job's own dead-man.
- **Time**: chrony (not timesyncd) — HMAC-signed API auth breaks
  silently on clock drift; slew, never step, after boot.
- **Disk**: journald capped (500M, keep-free 2G), logrotate on the
  JSONL journals, >80% disk = stop trading (fail closed, don't trade
  with an unwritable journal).
- **VPS choice**: Dublin or Amsterdam (~8–12ms to the matching engine
  in AWS eu-west-2; London IPs are geo-blocked, France too). Before
  committing to a provider, test the full authenticated flow from the
  actual IP — Cloudflare bot management 403s some datacenter ranges
  (Hetzner worst, DO/Vultr per-IP, small AWS eu-west-1 instances
  cleanest). A $5 node is fine: our measured stack needs <300MB RSS and
  the strategy needs sub-second, not sub-millisecond, execution.
- **Key security (live mode only)**: `systemd-creds encrypt` +
  `LoadCredentialEncrypted` (never env vars, never in the repo dir);
  dedicated wallet holding days of float only; hardcoded cold-address
  sweep; balance-change alarm in the housekeeping loop. This niche is
  actively targeted: Feb 2026 saw trojaned "Polymarket bot" repos
  exfiltrating `.env` private keys — never run cloned bot code
  unaudited, pin dependencies.

## The LLM's role (Hermes integration)

All 2024–2026 evidence says autonomous LLM traders lose money (Alpha
Arena: 4 of 6 frontier models lost >30% in two weeks) while
propose-only, human-gated LLM reflection is the working pattern. So:

- the numeric nightly loop uses **zero tokens** — profit is not spent
  on inference;
- optionally, a weekly Hermes/GEPA reflective pass reads the journals,
  the trial registry, and the digest, and drafts *natural-language*
  hypotheses ("weather flash frequency dropped after July 20 — likely
  a new competitor; consider raising min_profit in weather") as
  proposals through the same review queue. It has no write access to
  config, bounds, or gates — identical trust level as the numeric loop:
  none.

## Cost & expectation model

| item | cost |
|---|---|
| VPS (Dublin/Amsterdam) | ~$5–6/mo |
| alerting (ntfy + healthchecks free tiers) | $0 |
| numeric evolution | $0 (no tokens) |
| optional weekly LLM reflection | ~$2–10/run |
| measured paper edge (see README) | ~$10–50/day at retail caps |

Break-even is ~one captured basket per week. Everything beyond that is
data-compounding: every day of journals makes the evolution loop's
proposals more trustworthy, which is the actual "self-learning" — not
the config mutating nightly, but the *evidence base* growing until each
change is statistically defensible.

## Bootstrap sequence

1. `ops/polyarb/deploy.sh <repo-url>` on a fresh Debian VPS (paper mode).
2. Verify: `journalctl -u polyarb -f` shows detections; healthchecks green.
3. Let it run **≥2 weeks paper**. Read the nightly digests.
4. Review the first gated proposal; apply it; watch a week.
5. Only then, and only if your jurisdiction permits: wallet setup,
   `systemd-creds`, triple gate on, `--max-trade 25`. Compare live fills
   to paper for another two weeks before raising any cap.

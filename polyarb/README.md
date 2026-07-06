# polyarb — Polymarket negRisk arbitrage bot

A research-grounded arbitrage scanner and executor for Polymarket's CLOB.
Detects **structural basket arbitrage** in negative-risk (multi-outcome)
events, sizes it against real order-book depth net of 2026 taker fees,
and executes in paper mode by default — live trading sits behind a
triple environment gate plus a risk manager with hard caps and a kill
switch.

> **Honest expectations, up front.** "Perfect" (riskless) arbitrage on
> Polymarket exists, is measurable, and is heavily competed. The best
> peer-reviewed estimate (Saguillo et al., AFT 2025,
> [arXiv:2508.03474](https://arxiv.org/abs/2508.03474)) found **~$39.6M
> of realized arb profit in Apr 2024 – Apr 2025**, of which **~73%
> ($29M) was negRisk basket rebalancing** — exactly what this bot hunts.
> But that year included a US election; the top wallet made ~$2M with
> always-on sub-second infrastructure; by 2026, single-market
> opportunities persist a **median 3.6 seconds**
> ([arXiv:2605.00864](https://arxiv.org/abs/2605.00864)) and Polymarket
> introduced **taker fees explicitly designed to kill thin arb**
> (fee = shares × rate × p·(1−p), rates 0.03–0.07 by category;
> geopolitics 0). A REST-polling bot like this v0.1 will not outrun
> resident HFT bots on liquid books. Its realistic edge: the **long
> tail** — low-liquidity negRisk events, volatility spikes, fee-free
> categories — at $10s–$100s per episode. **Run `monitor` in paper mode
> for days and read `report` before ever going live.** The ledger tells
> you what edge actually existed; that is the only way to know whether
> live trading is +EV for you.

---

## The strategies (and the math)

A negRisk event = N mutually exclusive binary markets (election winner,
World Cup winner…). Exactly one resolves YES. Each market has its own
order book, so the books can drift out of joint consistency:

| Kind | Trade | Guaranteed payout | Fires when |
|------|-------|-------------------|-----------|
| `negrisk_long_yes` | BUY 1 YES in every outcome | $1 | Σ YES asks + fees < 1 − hurdle |
| `negrisk_long_no` | BUY 1 NO in every outcome | $(N−1) | Σ NO asks + fees < N−1 − hurdle (⇔ Σ YES bids > 1 + …) |
| `binary_long` | BUY YES + NO of one market | $1 | crossed book only — reported with a warning, never auto-traded |

Everything is **depth-walked**: the detector consumes ask ladders level
by level and only counts shares where the *marginal* basket cost (with
per-market fees) clears the hurdle, so reported size/profit is what the
books could actually fill at detection time.

Safety rules the detector enforces (each one is a real loss mode):

- **Completeness (long-YES):** payout is $1 only if the winner must be
  in your basket. Events with paused/unresolved outcomes outside the
  basket, or `negRiskAugmented` events without an "Other" market (a
  late-added candidate could win and pay you $0) are excluded — this is
  why apparent "41¢ arbs" on augmented events like *Nobel Peace Prize
  Winner* are refused: the missing probability mass IS the unlisted
  candidate. Long-NO is provably robust to both cases (an excluded or
  late-added winner makes *all* your NOs pay), so it stays enabled.
- **Fees:** per-market `feeSchedule` from Gamma
  (`fee = rate × (p(1−p))^exponent`, taker-only), verified live. A 1.5¢
  gross edge in a weather event with 3.6¢ of fees is not an arb.
- **Freshness:** books older than `max_book_age_s` are distrusted.
- **Mirror fact:** the NO book is the *exact* mirror of the YES book
  (verified level-by-level live — the CLOB matches complementary orders
  by minting/merging complete sets), so one book per market suffices and
  intra-market "YES+NO<$1" is treated as stale data, not free money.

Realization: hold to resolution, or exit early by merging complete
sets / converting NOs via the NegRiskAdapter
(`convertPositions` — N NOs → $(N−1)); both are on-chain calls outside
this v0.1 (see roadmap), so budget capital lock-up until resolution.

## Architecture

```
polyarb/
├── models.py      # MarketInfo, NegRiskEvent, OrderBook, Opportunity
├── arbmath.py     # FeeParams, depth-walking basket sizers (pure, tested)
├── gamma.py       # Gamma API: event/market discovery + completeness proof
├── clob.py        # CLOB API: batched order-book snapshots
├── detector.py    # prefilter (free Gamma quotes) -> walk books -> Opportunity
├── risk.py        # per-trade/daily caps, cooldowns, kill-switch file
├── execution.py   # PaperExecutor / LiveExecutor (FAK legs, py-clob-client-v2)
├── ledger.py      # JSONL ledger + profitability report
├── scanner.py     # orchestration loop
└── cli.py         # scan | monitor | run | report
```

One scan cycle against live APIs: ~150 negRisk events discovered, ~80
past the prefilter, ~850 books fetched (batched 100/POST, well inside
the 500-per-10s limit), full detection in **~3 seconds**.

## Usage

Scanning needs only `requests` (no keys, no wallet, no geo restrictions
— market data is public):

```bash
pip install requests

python -m polyarb scan                          # one-shot
python -m polyarb monitor --interval 5          # continuous detection
python -m polyarb run --paper --interval 5      # + simulated executions
python -m polyarb report                        # what edge existed, when, how big
```

Useful knobs (see `--help`): `--min-edge` (per-share $ after fees,
default 0.01), `--min-profit` (absolute $, default 0.50),
`--max-notional`, `--max-legs`, `--allow-augmented`, `--min-liquidity`,
`--max-events`.

### Live trading (only after paper data says yes)

```bash
pip install py-clob-client-v2        # v1 client was archived May 2026

export POLYMARKET_PRIVATE_KEY=0x...  # signer EOA key
export POLYMARKET_SIGNATURE_TYPE=1   # 0=EOA 1=email/Magic 2=Safe 3=deposit wallet
export POLYMARKET_FUNDER=0x...       # proxy/Safe/deposit address holding funds

# the triple gate — all three required, deliberately annoying
export POLYARB_MODE=live
export LIVE_TRADING_ENABLED=true
export DRY_RUN=false

python -m polyarb run --live --max-trade 25 --max-daily 100
```

Live prerequisites (the bot does **not** do these for you):

1. **Funding + allowances.** Fund the funder wallet (pUSD/USDC.e as
   applicable) and grant the standard approvals to the CTF exchanges and
   NegRiskAdapter once (Polymarket's UI/relayer does this for
   email/deposit wallets automatically).
2. **API creds** are derived programmatically from your key on startup
   (`create_or_derive_api_key`) — never copy keys from the website UI;
   frontend-created keys fail signature validation for proxy wallets.
3. **Geo/ToS.** Order placement on the global CLOB is refused from the
   US, UK, FR and other close-only regions (`GET
   polymarket.com/api/geoblock`), and datacenter IPs are frequently
   challenged by Cloudflare. Where you run this, and whether you may, is
   your responsibility.
4. **Legging risk is yours.** Legs are concurrent FAK orders; there is
   no atomic basket on Polymarket. On a partial basket the bot writes
   the kill-switch file (`polyarb.KILL`) and halts so a human decides
   whether to complete or unwind. Delete the file to resume.

## Measured results (paper, July 6 2026, one hour, live markets)

Two simultaneous paper runs over the same market window, same thresholds
(min edge 0.2¢/share net of fees, min profit $0.05, $250 notional cap,
800 events ≈ 180–236 negRisk events monitored):

| | REST poller | WebSocket engine (`--ws`) |
|---|---|---|
| window | 60 min | 43 min |
| effective detection latency | ~7.3s/cycle (2,316 books/cycle) | ~0.1s after a book change |
| raw detections → episodes | 496 → **2** | 381 → **7** |
| realizable profit (1 fill/episode, depth-true, net of fees) | **$5.21** | **$19.78** |
| WS reconnects | — | 0 (407k events) |

What the episodes were:

- **Persistent fee-free geopolitics baskets** (both runs caught these):
  "How many countries will Israel strike in 2026" — 12-leg long-YES
  basket at 4.7–5.5% ROI that persisted the entire hour. Slow capital,
  real edge, resolution-wording risk, locked until resolution.
- **Flash mispricings** (WS-only): daily weather-bucket events
  ("Highest temperature in Ankara") repricing violently — episodes
  lasting 0–9 seconds at 1–18% ROI. A multi-second poller structurally
  cannot trade these; this class is why the WS engine exists.
- Baseline efficiency: of 180 events tightness-tracked every cycle for
  the hour, only 2 ever went net-positive at top-of-book. The market is
  picked clean where bots watch; the edge is in breadth + speed + fee
  awareness.

Honest read: ~$20–27/hour of *detected, depth-true, fee-net* paper edge
at retail caps during one random Monday hour. Paper fills are optimistic
(no slippage, no legging failures, no competition for the same depth),
weather/geopolitics flow varies by news cycle, and long-YES baskets lock
capital to resolution. Treat this as an upper bound and a reason to
collect more data, not a promise.

## Measuring profitability (the actual "find a way" loop)

1. `python -m polyarb monitor --min-edge 0.003 --min-profit 0.05` for
   1–2 weeks (a $5/mo VPS is fine). Every detection is journaled to
   `polyarb_data/opportunities.jsonl` with legs, depth-true size, fees
   and profit; every cycle to `scans.jsonl`.
2. `python -m polyarb report` — frequency × size × ROI by arb kind. If
   weekly theoretical profit at realistic caps doesn't clear your
   infra + capital-lockup costs, don't go live; that is a valid result.
3. If it does: `run --paper` first (adds execution simulation), then
   `--live` with tiny caps (`--max-trade 25`), comparing realized fills
   against paper. Scale only if live ≈ paper.

## Roadmap to a sharper edge

Ordered by measured impact per unit of work:

- ~~WebSocket books~~ — **done** (`--ws`): sharded live L2 books,
  event-driven detection; measured 3.8× the realizable episode profit
  of REST polling on the same window (see results above).
- **Pre-signed order ladders** — V2 orders are pre-signable (no nonce,
  ms timestamp); Python EIP-712 signing costs ~1s/order, so the live
  hot path must be HMAC + one batched HTTP/2 POST (`POST /orders`, ≤15
  legs). This is the gap between detecting a 9-second flash and filling
  it. See ADVANCED.md.
- **NegRiskAdapter `convertPositions`** — realize long-NO baskets
  immediately instead of waiting for resolution (capital efficiency was
  the top wallet's core trick; "buying NO" was the single most
  profitable strategy class at $17.3M/yr). Verified on-chain: adapter
  fee 0, gas ≈165k per complement outcome (~$0.35 worst case). The
  corridor is heavily botted (~967 conversions observed in 3h).
- **CTF merge** for early exit of complete sets.
- **Maker-side capture** — post inside the spread on both sides of
  near-inconsistent baskets so the *other* side pays the taker fee and
  you collect the 20–25% maker rebate; this is where fee-era arb
  economics actually favor you.
- **Heartbeat dead-man switch** (`/heartbeat` API) so a crashed bot's
  resting orders auto-cancel.
- Cross-venue (Kalshi/Polymarket-US) arb only with a manually verified
  contract-equivalence whitelist — resolution-criteria mismatch turns
  "riskless" into directional (see the Zelenskyy-suit UMA dispute).

## Risk disclaimers

Resolution risk (UMA disputes can settle "obvious" markets the other
way), augmented-event candidate additions, partial fills, API/WS
staleness, geoblocking, smart-contract risk, and fee-schedule changes
all apply. Hold-to-resolution locks capital for weeks. Nothing here is
financial advice; the safety rails default to *off* for live trading
for a reason. Start in paper mode, measure, and size small.

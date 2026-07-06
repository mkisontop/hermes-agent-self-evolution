# polyarb — Advanced strategy & latency research digest (July 2026)

Condensed from primary-source research (docs, contract source, papers,
live API/WS experiments). Everything here informed or will inform the
code; items marked ROADMAP are not implemented yet.

## 1. Latency engineering (what "low latency" actually means here)

- **Matching engine**: centralized, off-chain, hosted in **AWS eu-west-2
  (London)** behind Cloudflare. Fast actors deploy in **eu-west-1
  (Dublin)** (UK IPs are geo-blocked): 0.5–1.5ms TCP to the edge, <10ms
  order POST RTT vs ~130ms from US East.
- **WebSocket feed** (implemented in `ws.py`): initial full-book dump
  arrives as one JSON *array* of `book` events ~1 RTT after subscribe;
  `price_change` carries the **new total size per level** (0 removes),
  plus `best_bid/best_ask` per change; updates arrive for both outcome
  tokens (we mirror NO→YES; verified exact). **Hard cap ~500
  assets/connection and it fails silently above** (snapshots stop,
  deltas continue) — we shard at 300. No compression is negotiated.
- **Consistency**: no sequence numbers. The only primitive is the book
  `hash` (SHA1 over compact JSON with a documented key order, seeded
  with `min_order_size`/`neg_risk` from REST). ROADMAP: hash-chain
  verification with re-snapshot on divergence; today we conservatively
  re-snapshot on every reconnect and distrust unhealthy connections.
- **Order path**: CTF Exchange **V2** (cutover 2026-04-28, pUSD
  collateral): signed struct is `salt, maker, signer, tokenId,
  makerAmount, takerAmount, side, signatureType, timestamp(ms),
  metadata, builder` — **no nonce, no expiration in the struct**, so
  orders are **pre-signable**. Python EIP-712 signing costs ~1s/order in
  py-clob-client-v2 → ROADMAP: pre-sign price/size ladders per token so
  the hot path is HMAC + one HTTP/2 POST on a warm session. `POST
  /orders` batches up to 15 orders (a whole basket in one round trip).
- **Taker delays (why raw speed no longer wins everywhere)**: 250ms
  non-cancellable hold on crypto/finance up-down markets (funds
  reserved, duplicates rejected), **3s delay on marketable sports
  orders**, 1s being tested on NBA/MLB. Geopolitics: no delay, no fees.
- **Dead-man switch**: `POST /heartbeats` (L2 auth) every 5s carrying
  the last `heartbeat_id`; miss >10s (+5s buffer) → all open orders
  auto-cancelled. Arm this before any maker strategy. (ROADMAP)
- **Rate limits**: POST /order ~5,000/10s burst but design to ~60/s
  sustained; batch endpoint multiplies throughput 15×. Real "ban" risk
  is Cloudflare bot-management on datacenter IPs — keep cookie jar,
  persistent session, or get whitelisted via Discord.

## 2. Where post-fee edge lives (strategy map)

Fee formula: `fee = shares × rate × p(1−p)`, taker-only; maker rebate
20–25% of the pool. Rates: sports 0.03, politics/finance/tech 0.04,
economics/culture/weather 0.05, crypto 0.072, **geopolitics 0**. Fees
only on markets deployed after activation (`feesEnabled`) — legacy
long-dated books remain fee-free.

| Strategy | Status in polyarb | Note |
|---|---|---|
| Taker negRisk baskets (LONG_YES / LONG_NO) | **implemented** | edge survives in fee-free geopolitics + legacy markets and at extreme prices where p(1−p)→0; our first live paper hits were exactly this (geopolitics count-ladders at ~4.7% ROI) |
| convertPositions realization for LONG_NO | ROADMAP (web3 direct; no SDK support) | same trigger as LONG_NO, ~N× capital efficiency (Polymarket's own docs cite up to 9.5×): buy NOs on subset S, convert → YES on complement + (\|S\|−1) cash, sell YES. Optimal S is separable and greedy-exact at top of book: include i iff ask(NO_i) < 1 − bid(YES_i). On-chain facts (verified July 2026): adapter `feeBips = 0` on live markets incl. World Cup; gas ≈165k per complement outcome (60-outcome convert ≈ 10.4M gas ≈ $0.35 at POL $0.07); post-V2, call the NegRiskCtfCollateralAdapter to receive pUSD directly. Competition: ~967 PositionsConverted events observed in a 3h window — the corridor is heavily botted |
| Liquidity-rewards farming | ROADMAP (pairs with maker capture) | separate program from rebates: quadratic order score ((v−s)/v)²·b inside the max-spread band, sampled ~1/min, paid daily, $1 min; double-sided quoting required at extreme midpoints, ~1/3 credit single-sided otherwise; per-market `rewards` object in the CLOB API. Historical solo-operator income $200–800/day on $10k (2024–25, pre-competition); decayed toward ~10% APY-style in 2026 |
| Maker-side basket capture | ROADMAP (big) | quote resting bids across an event s.t. full fill = arb basket; earn rebates instead of paying fees; needs unbalanced-fill machinery (inventory skew, toxicity/markout-EWMA quote pulling, convert as escape hatch). poly-maker v2 is the reference architecture: post-only everything, 200ms debounce, regime machine (QUIET/TRENDING/EVENT/REDUCE_ONLY/HALTED), and an event-group worst-case loss cap — the only public negRisk maker-risk implementation. The N-leg *basket capture* variant has no public write-up at all (genuine gap = genuine opportunity, and genuinely untested). Rebates: 20% (crypto) / 25% (others) of the taker pool, fee-curve weighted **per market**, paid daily, only on filled orders. Practitioner replications of vanilla single-market MM report net ≈ $0 in 2026 competition; adverse selection on jump moves is the dominant cost |
| Endgame sniping (0.97–0.995 near-certainties) | not planned (directional) | fees are negligible at extremes (~4–7bps) but UMA dispute tail risk (−100%) dominates; practitioner discipline is sell at 0.95+, never hold ambiguous rules |
| New-market sniping | ROADMAP (cheap) | `new_market` WS event (custom_feature_enabled) + recurring series have predictable creation times; be first maker at fat spreads |
| 15-min crypto latency arb | dead by design | dynamic taker fee (~3.15% peak) + 250ms hold introduced precisely to kill it |
| Cross-event logical arb (P(A)≤P(B) containment, count-ladders vs cumulative) | ROADMAP | live confirmed structures: `world-cup-winner` (negRisk) vs `world-cup-nation-to-reach-semifinals` (non-negRisk team legs); Fed rate-cut count ladder. Trap: rules-text mismatch (e.g. "Other if tournament incomplete by Oct 13") breaks implications; needs rules diffing before trading. Fees ~1–1.25¢/leg at mid — demand >2.5–3¢ |
| Whale/copy-trading | rejected | arb edge is consumed in the originating fill; 5–15s alert latency > edge lifetime. Useful only as a toxicity signal for maker quoting |

## 3. Empirical grounding

- ~$39.6M realized arb profit Apr 2024–Apr 2025; **73% from negRisk
  rebalancing**; top wallet ~$2M/yr; combinatorial ~$95K total
  (Saguillo et al., AFT 2025, arXiv:2508.03474).
- 2026 single-market arb: ~7 executable episodes/month in liquid NBA
  books, median persistence 3.6s (arXiv:2605.00864).
- Our own measurements (July 2026): REST full-universe cycle ~3s for
  ~850 books; WS detection fires ~0.1–0.2s after a book change;
  fee-free geopolitics count-ladder baskets with 1–5% ROI persist for
  **minutes**, i.e. in the long tail *breadth beats speed* — the
  binding constraints there are leg count, capital lock-up to
  resolution, and resolution-wording risk, not latency.

## 4. Design consequences already in the code

1. WS-driven event detection (`--ws`) with mirrored books, health-gated
   snapshots, 300-asset shards, liveness watchdog + reconnect.
2. Fee-aware walkers using the verified `feeSchedule` objects.
3. Long-YES completeness proof (augmented events / paused legs / early
   resolutions) — refuses the fake "41¢ arbs" whose missing mass is an
   unlisted outcome.
4. Episode-deduped reporting (a persisting opportunity is one chance to
   trade, not N).
5. Risk defaults tuned for paper honesty: 900s event cooldown because a
   real fill consumes the mispricing while a paper fill does not.

# 🧬 Hermes Agent Self-Evolution

**A proposal-first, safety-hardened self-improvement engine for [Hermes Agent](https://github.com/mkisontop/hermes-agent).**

This engine evolves Hermes skills using **DSPy + MIPROv2**, evaluates candidates on holdout data, writes proposals with integrity manifests, and only ever writes back through an explicitly hardened reviewer path.

It is now designed to be used as a **nightly proposal engine** first, and only later as a cautious auto-merge engine once additional trust-building gates are added.

---

## Current Operating Mode

### Ready now
- ✅ nightly proposal generation
- ✅ manual proposal review
- ✅ hardened manual approval / write-back path
- ✅ MIPRO production path
- ✅ judge containment
- ✅ manifest hashes + stale-baseline guard
- ✅ atomic write-back safety
- ✅ risk tiers
- ✅ nightly digest generation

### Not enabled yet
- ❌ unattended real auto-merge by default

This is intentional.

The current engine is strong for **proposal generation and manual approval**, but real unattended auto-merge should stay off until the next safety batch (paired-win, dry-run, quarantine, nightly merge budget, low-risk-only real merge) is implemented and validated.

---

## Safety Model

This repo now has three layers of safety.

### Batch A — foundation hardening
- dependency lock with hashes (`requirements.lock`)
- self-evolution hard-block / denylist
- doctor checks for routing, packages, scheduler, self-block, faulthandler, live pings
- production model routing:
  - `codex-spark` = optimizer / proposer / reflection / task
  - `gpt-5.4` = judge / eval only
- `faulthandler` before `SIGALRM`

### A-prime — judge containment
- role-aware LM factory (`task`, `optimizer`, `judge`)
- judge-only timeout / retries / token caps
- judge phase timeout
- judge canary via `python -m evolution.doctor_config --judge-canary <skill>`
- proposal preserved when judge fails (`judge_failed=true`, `auto_merge=false`)

### Batch B — approval/write-back hardening
- `manifest.json` with SHA256 of baseline / evolved / diff
- stale-baseline approval guard
- tampered-artifact guard
- risk tiers (`low`, `medium`, `high`, `critical`)
- root guard + symlink refusal + same-device atomic write
- rollback-capable write-back path
- reviewer-side refusal codes and force-flag separation

---

## Architecture

```text
skill text
  -> synthetic / imported eval dataset
  -> MIPROv2 optimization (proposal-first)
  -> holdout judge scoring
  -> AutoMergeGate decision
  -> proposal bundle + manifest
  -> human review via proposal_reviewer
  -> hardened write_back_skill path (only when approved)
```

Everything is built around the principle:

> **Generate proposals first. Review before live writes.**

---

## Key Files

### Core engine
- `evolution/skills/evolve_skill.py` — main optimization pipeline
- `evolution/core/lm_factory.py` — role-aware LM construction
- `evolution/core/regression_guard.py` — gate decision logic
- `evolution/core/manifest.py` — proposal integrity hashes
- `evolution/core/risk.py` — risk tier policy
- `evolution/core/write_back.py` — hardened live write-back
- `evolution/review/proposal_reviewer.py` — human approval / reject CLI
- `evolution/review/digest.py` — proposal digest generation
- `evolution/doctor_config.py` — health and routing verification

### Runtime state
- `logs/` — nightly / evolve / smoke / digest logs
- `proposals/<skill>/<timestamp>/` — reviewable proposals
- `output/<skill>/<timestamp>/` — raw run artifacts
- `state/` — reserved for future runtime state (Batch C)

---

## Model Routing Policy

Current safe routing is:

- **Optimizer / proposer / reflection / task:** `openai/cx/gpt-5.3-codex-spark`
- **Judge / eval:** `openai/cx/gpt-5.4`

Do **not** switch `gpt-5.4` back into proposer/optimizer roles by default.

That path was explicitly separated because proposer-shaped prompts were the unstable part of the earlier system.

---

## Quick Start

```bash
cd ~/.hermes/self-evolution
source venv/bin/activate
python -m evolution.doctor_config --live
```

If all green, start using the engine in **proposal mode**.

### One supervised top-1 run

```bash
cd ~/.hermes/self-evolution

ALLOW_EVOLVE=1 \
SKIP_EVOLVE=0 \
SKILL=writing-plans \
OPTIMIZER=mipro \
EVOLUTION_AUTO_MERGE=0 \
./nightly.sh
```

Expected:
- smoke passes
- proposal written
- manifest written
- digest written
- live skill untouched

### Daily review loop

```bash
cd ~/.hermes/self-evolution
python -m evolution.doctor_config
python -m evolution.review.proposal_reviewer list
cat logs/digests/$(date +%F).md
```

Inspect a proposal:

```bash
python -m evolution.review.proposal_reviewer show writing-plans 20260422_153011
python -m evolution.review.proposal_reviewer diff writing-plans 20260422_153011
```

Approve only through the reviewer:

```bash
python -m evolution.review.proposal_reviewer approve writing-plans 20260422_153011
```

Reject noisy ones:

```bash
python -m evolution.review.proposal_reviewer reject writing-plans 20260422_153011 --reason "not clearly better"
```

Never copy files by hand.

---

## Nightly Mode

The nightly pipeline is driven by `nightly.sh`.

Default phases:
1. smoke preflight (`t1` + `t5`)
2. evolve selected skills in proposal mode
3. build digest

### Important defaults
- `OPTIMIZER=mipro`
- `EVOLUTION_FITNESS_MODE=fast`
- `EVOLUTION_HOLDOUT_METRIC=judge`
- `EVOLUTION_AUTO_MERGE=0`

### Recommended current production posture

```bash
export ALLOW_EVOLVE=1
export EVOLUTION_AUTO_MERGE=0
export OPTIMIZER=mipro
export EVOLUTION_FITNESS_MODE=fast
export EVOLUTION_HOLDOUT_METRIC=judge
```

That means:
- proposals are generated nightly
- digests are written nightly
- no live writes happen automatically

---

## Reviewing Proposal Quality

A positive score delta alone is **not enough**.

Good signs:
- shorter or cleaner skill
- improved ordering
- more specific instructions
- edge cases added without bloat
- no safety loss
- score stable or improved

Bad signs:
- large rewrite with tiny gain
- verbosity without value
- generic rules replacing precise guidance
- important caveats removed
- obvious overfitting to examples
- frontmatter / structure degradation

The current philosophy is:

> **Read the diff. Then decide.**

---

## Approval Safety Guarantees

When you approve through `proposal_reviewer`, the path now enforces:
- live hash matches manifest baseline
- evolved artifact hash matches manifest
- risk tier is allowed
- write target is inside allowed roots
- no symlink escape
- backup created before overwrite
- atomic replace path
- rollback-capable write-back

After approval, always run:

```bash
pytest -q
./smoke_test.sh t1
./smoke_test.sh t5
python -m evolution.doctor_config
```

Then confirm the backup exists:

```bash
find ~/.hermes/hermes-agent -path '*backups*' -type f | tail -20
```

---

## Current Readiness

A realistic rating right now:

| Area | Rating |
|------|-------:|
| Nightly proposal generation | 8.5/10 |
| Manual approval safety | 9/10 |
| Write-back safety | 8.5–9/10 |
| Model routing reliability | 8.5/10 |
| Judge containment | 8/10 |
| Fully unattended auto-merge | 6/10 (not enabled) |

So yes — it is worth using now.

But use it in the right mode:
- **proposal-first**
- **manual approval**
- **no real unattended auto-merge yet**

---

## Roadmap: Next Batch

The next engineering batch should add:
- paired-win evaluation
- severe-regression detection
- auto-merge dry-run mode
- quarantine after repeated failures
- max one auto-merge per night
- low-risk-only real auto-merge
- digest would-merge / would-not-merge reasons

That is what turns this from a:

> nightly proposal engine

into a:

> cautious low-risk auto-merge engine

Until then, manual approval is the correct mode.

---

## Status

This repo is now a **serious self-evolution appliance**.

It is no longer “experimental glue that sometimes proposes things.”
It is a structured, proposal-first, risk-aware, reviewable engine designed to improve Hermes safely over time.

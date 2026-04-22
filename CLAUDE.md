# Hermes Self-Evolution — Claude Code Rules

You are operating inside the Hermes self-evolution engine.

## Hard boundaries

- Do not read `.env`, `.env.*`, `secrets/**`, `~/.ssh/**`, `~/.aws/**`, or `~/.hermes/secrets/**`.
- Do not use or request `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, or Console API credentials.
- Do not touch live Hermes Agent skills except through the approved proposal reviewer / write-back path.
- Do not use `--force-stale`, `--force-tampered`, `--force-critical`, or `--allow-no-manifest` unless the user explicitly asks for emergency recovery.
- Do not evolve `hermes-self-evolution` automatically.

## Current engine state

The safe commit ladder is:

- `14d31a1` Harden self-evolution foundation (Batch A)
- `d70342a` Contain judge-phase failures in evolution runs (A-prime)
- `70f4200` Add manifest + risk tiers + atomic write-back hardening (Batch B)
- `71789a6` Fix nightly.sh supervised-run wiring
- `6f5c835` Bump EVOLUTION_TASK_RETRIES 0 → 1 in .env.example

MIPRO is the production optimizer. GEPA is experimental.
`codex-spark` is optimizer / proposer / reflection / task.
`gpt-5.4` is judge / eval **only**.

## Required verification

For code changes, run:

```
pytest -q
./smoke_test.sh t1
./smoke_test.sh t5
python -m evolution.doctor_config
```

When judge routing is affected, also run:

```
python -m evolution.doctor_config --live
python -m evolution.doctor_config --judge-canary writing-plans
```

## Coding policy

- Prefer small, bisectable commits.
- Preserve proposal-first behavior.
- Do not enable real auto-merge unless the user explicitly asks after Batch C.
- Add tests for every safety invariant.
- Do not call `git push`, `rm -rf`, `sudo`, or pipe network content to interpreters.

## Batch C scope (when asked to implement)

1. Paired-win holdout evaluation with randomized A/B order
2. Severe-regression detection
3. `EVOLUTION_MIN_AUTO_DELTA=0.05` enforcement
4. `EVOLUTION_JUDGE_SIGMA_OVERRIDE` support
5. Auto-merge dry-run mode
6. Quarantine state file after repeated failures
7. Max one auto-merge per night
8. Low-risk-only real auto-merge
9. Digest would-merge / would-not-merge reasons
10. Tests for every gate above

Do not enable real auto-merge by default.

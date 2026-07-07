"""Hermes reflective pass: LLM-as-scientist over the trading journals.

    python -m evolution.trading.reflect \
        --baseline /var/lib/polyarb/polyarb.json \
        --data-dir /var/lib/polyarb/data [--dry-run]

The third loop of the autonomy stack (see AUTONOMY.md). A frontier LLM
reads the pre-digested drift evidence (never raw journals) through the
`polyarb-analyst` skill prompt and returns structured analysis. Its
config suggestion — if any — is treated with ZERO trust:

    clamp to gene bounds -> score by journal replay -> same walk-forward
    constraints and AutoMergeGate as the numeric loop -> PENDING
    proposal for human review (never auto-applied)

so a hallucinated or adversarial suggestion is mechanically incapable of
reaching the live config. What the LLM uniquely adds over the numeric
loop is *causal* reasoning: regime diagnosis, competitor detection,
"why" narratives, and escalation judgment.

The prompt lives in evolution/trading/skills/polyarb-analyst/SKILL.md —
a standard skill file, so the existing GEPA skill-evolution machinery
can evolve the analyst itself (fitness: were its proposals gate-passing
and human-approved?). Loops all the way down; money at the bottom of
none of them.

Cost control: at most one reflection per --min-interval-days (spend
ledger in the data dir), packet capped in size, model/key via env:
OPENAI_API_KEY, POLYARB_LLM_MODEL, POLYARB_LLM_BASE_URL.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import replace
from pathlib import Path

from evolution.core.proposals import ProposalWriter, build_proposal_record
from evolution.core.regression_guard import AutoMergeGate

from polyarb.tuning import TradingConfig, load_config

from .genome import GENE_BOUNDS, bounds_for, validate
from .replay import load_episodes, walk_forward
from .signals import compute_drift

log = logging.getLogger("reflect")

SKILL_PATH = Path(__file__).parent / "skills" / "polyarb-analyst" / "SKILL.md"
MAX_PACKET_CHARS = 24_000
MAX_GENES_PER_SUGGESTION = 3


# ---------------------------------------------------------------------------
# packet assembly
# ---------------------------------------------------------------------------


def _recent_proposals(proposals_dir: str, limit: int = 5) -> list[dict]:
    root = Path(proposals_dir) / "polyarb-config"
    out = []
    if not root.exists():
        return out
    for pdir in sorted((p for p in root.iterdir() if p.is_dir()), reverse=True)[:limit]:
        try:
            status = (pdir / "STATUS").read_text().strip()
            decision = json.loads((pdir / "decision.json").read_text())
            out.append({
                "timestamp": pdir.name,
                "status": status,
                "improvement": decision.get("improvement"),
                "gate_reason": decision.get("gate_reason"),
                "engine": decision.get("metadata", {}).get("engine"),
            })
        except (OSError, ValueError):
            continue
    return out


def _trials_total(proposals_dir: str) -> int:
    path = Path(proposals_dir) / "polyarb-config" / "trials.jsonl"
    if not path.exists():
        return 0
    with open(path, encoding="utf-8") as f:
        return sum(1 for _ in f)


def build_packet(baseline: TradingConfig, data_dirs: list[str],
                 proposals_dir: str) -> dict:
    drift = compute_drift(data_dirs)
    packet = {
        "drift": drift.to_dict(),
        "current_config": json.loads(baseline.to_json()),
        "gene_bounds": {
            g: {"lo": lo, "hi": hi} for g, (lo, hi, _) in bounds_for(baseline).items()
        },
        "recent_proposals": _recent_proposals(proposals_dir),
        "trials_total": _trials_total(proposals_dir),
    }
    raw = json.dumps(packet)
    if len(raw) > MAX_PACKET_CHARS:  # truncate oldest days first
        while len(json.dumps(packet)) > MAX_PACKET_CHARS and packet["drift"]["days"]:
            packet["drift"]["days"].pop(0)
    return packet


# ---------------------------------------------------------------------------
# LLM call + response validation
# ---------------------------------------------------------------------------


def call_llm(packet: dict, client=None, model: str | None = None) -> dict:
    """Send skill prompt + packet, return parsed JSON analysis.

    ``client`` is injectable for tests; default is the OpenAI SDK
    against OPENAI_API_KEY / POLYARB_LLM_BASE_URL / POLYARB_LLM_MODEL.
    """
    system = SKILL_PATH.read_text(encoding="utf-8")
    user = json.dumps(packet, separators=(",", ":"))
    if client is None:
        from openai import OpenAI  # deferred: heavy import

        client = OpenAI(base_url=os.environ.get("POLYARB_LLM_BASE_URL") or None)
    model = model or os.environ.get("POLYARB_LLM_MODEL", "gpt-5.4")
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "system", "content": system},
                  {"role": "user", "content": user}],
        temperature=0.2,
    )
    text = resp.choices[0].message.content or ""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError(f"analyst returned no JSON object: {text[:200]!r}")
    return json.loads(text[start:end + 1])


def sanitize_suggestion(
    analysis: dict, baseline: TradingConfig
) -> tuple[TradingConfig | None, list[str]]:
    """Zero-trust conversion of the LLM's suggestion into a candidate.

    Unknown genes dropped, values clamped into bounds, >3 changes
    truncated. Returns (candidate config or None, notes)."""
    notes: list[str] = []
    sug = analysis.get("config_suggestion")
    if not sug or not isinstance(sug, dict) or not sug.get("genes"):
        return None, ["no config suggestion"]
    b = bounds_for(baseline)
    kwargs = {}
    for gene, value in list(sug["genes"].items())[:MAX_GENES_PER_SUGGESTION]:
        if gene not in GENE_BOUNDS:
            notes.append(f"dropped unknown gene {gene!r}")
            continue
        lo, hi, is_int = b[gene]
        try:
            v = float(value)
        except (TypeError, ValueError):
            notes.append(f"dropped non-numeric {gene}={value!r}")
            continue
        clamped = max(lo, min(hi, v))
        if clamped != v:
            notes.append(f"clamped {gene} {v} -> {clamped}")
        kwargs[gene] = int(round(clamped)) if is_int else clamped
    if not kwargs:
        return None, notes or ["suggestion empty after sanitization"]
    cand = replace(baseline, **kwargs)
    problems = validate(cand, baseline)
    if problems:  # defense in depth; should be unreachable after clamping
        return None, notes + [f"validation failed: {problems}"]
    return cand, notes


# ---------------------------------------------------------------------------
# orchestration
# ---------------------------------------------------------------------------


def _spend_guard(data_dir: str, min_interval_days: float) -> bool:
    path = os.path.join(data_dir, "llm_spend.jsonl")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
        if lines:
            last = json.loads(lines[-1])
            age_days = (time.time() - last["ts"]) / 86400
            if age_days < min_interval_days:
                log.warning(
                    "last reflection %.1f days ago (< %.1f) — skipping "
                    "(use --force to override)", age_days, min_interval_days,
                )
                return False
    return True


def _record_spend(data_dir: str, model: str, packet_chars: int) -> None:
    with open(os.path.join(data_dir, "llm_spend.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": time.time(), "model": model,
                            "packet_chars": packet_chars}) + "\n")


def run_reflection(args, client=None) -> int:
    baseline = load_config(args.baseline)
    packet = build_packet(baseline, args.data_dir, args.proposals_dir)
    report_dir = Path(args.data_dir[0])

    if args.dry_run:
        print(json.dumps(packet, indent=2)[:4000])
        print(f"\npacket: {len(json.dumps(packet))} chars, dry run — no LLM call")
        return 0
    if not args.force and not _spend_guard(args.data_dir[0], args.min_interval_days):
        return 0

    model = args.model or os.environ.get("POLYARB_LLM_MODEL", "gpt-5.4")
    analysis = call_llm(packet, client=client, model=model)
    _record_spend(args.data_dir[0], model, len(json.dumps(packet)))

    # always persist the narrative for the human digest
    ts = time.strftime("%Y%m%d_%H%M%S")
    report_path = report_dir / f"reflection-{ts}.md"
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(f"# polyarb reflection {ts}\n\n{analysis.get('summary', '')}\n\n")
        for a in analysis.get("anomalies", []) or []:
            f.write(f"- **{a.get('severity', '?')}** {a.get('signal', '')}: "
                    f"{a.get('evidence', '')}\n")
        for h in analysis.get("hypotheses", []) or []:
            f.write(f"- hypothesis: {h}\n")
        if analysis.get("escalate_to_human"):
            f.write(f"\n**ESCALATION**: {analysis.get('escalation_reason', '')}\n")
    print(f"reflection report: {report_path}")
    if analysis.get("escalate_to_human"):
        print(f"ESCALATION: {analysis.get('escalation_reason', '')}")

    # zero-trust path for the config suggestion
    cand, notes = sanitize_suggestion(analysis, baseline)
    if cand is None:
        print(f"no proposal ({'; '.join(notes)})")
        return 0
    episodes = load_episodes(args.data_dir)
    if not episodes:
        print("suggestion received but no journal episodes to validate it — dropped")
        return 0
    base_train, base_hold, wf_warnings = walk_forward(baseline, episodes)
    cand_train, cand_hold, _ = walk_forward(cand, episodes)

    class _C:
        def __init__(s, name, passed, message):
            s.constraint_name, s.passed, s.message = name, passed, message

    constraints = [
        _C("bounds", True, "sanitized + clamped: " + ("; ".join(notes) or "clean")),
        _C("walk_forward_data", not wf_warnings,
           wf_warnings[0] if wf_warnings else "ok"),
        _C("holdout_non_regression",
           bool(wf_warnings) or cand_hold.fitness >= base_hold.fitness - 1e-9,
           f"holdout ${cand_hold.fitness:.2f} vs ${base_hold.fitness:.2f}"),
    ]
    decision = AutoMergeGate(min_improvement=args.min_improvement).evaluate(
        base_train.fitness, cand_train.fitness, all(c.passed for c in constraints)
    )
    record = build_proposal_record(
        skill_name="polyarb-config",
        baseline_text=baseline.to_json(),
        evolved_text=cand.to_json(),
        baseline_score=base_train.fitness,
        evolved_score=cand_train.fitness,
        decision=decision,
        constraint_results=constraints,
        mode="propose",  # reflective suggestions are NEVER auto-applied
        metadata={
            "engine": "hermes-reflective",
            "model": model,
            "rationale": (analysis.get("config_suggestion") or {}).get("rationale", ""),
            "sanitizer_notes": notes,
            "summary": analysis.get("summary", ""),
        },
    )
    path = ProposalWriter(args.proposals_dir).write(record)
    print(f"reflective proposal written: {path} "
          f"(train ${base_train.fitness:.2f} -> ${cand_train.fitness:.2f}, "
          f"gate: {decision.reason})")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="reflect")
    p.add_argument("--baseline", required=True)
    p.add_argument("--data-dir", action="append", required=True)
    p.add_argument("--proposals-dir", default="proposals")
    p.add_argument("--model", default=None)
    p.add_argument("--min-improvement", type=float, default=0.5)
    p.add_argument("--min-interval-days", type=float, default=6.0)
    p.add_argument("--force", action="store_true")
    p.add_argument("--dry-run", action="store_true",
                   help="print the evidence packet, no LLM call, no spend")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    return run_reflection(args)


if __name__ == "__main__":
    sys.exit(main())

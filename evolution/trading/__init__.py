"""Phase 4b: trading-config evolution for the polyarb daemon.

Evolves the bounded TradingConfig genome against the daemon's own
journals (offline replay, zero LLM tokens), walk-forward validated,
emitted as propose-mode proposals through the same review pipeline as
skill evolution. Execution code and risk ceilings are not evolvable.
"""

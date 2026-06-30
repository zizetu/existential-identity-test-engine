"""
benchmarks/ - tical-code benchmark adapter layer

Non-invasive, pure additive module.
Each adapter handles: load benchmark data → convert to tical-code tool-calling format → execute → validate → score.

Adapted benchmarks:
- BFCL v3: Tool-calling accuracy (Berkeley Function-Calling Leaderboard)
- τ²-Bench: Multi-turn dialogue + strategy compliance (Sierra Research)
- Terminal Bench 2.0: Terminal tasks (CMU/Sierra)
- GAIA: General assistant multi-step reasoning (Meta + HuggingFace)
"""

# Deprecated Modules — Historical Reference

These modules were designed as independent prototypes but were superseded
by better implementations during active development. They are preserved
here for archaeology, not wired into the runtime.

| File | Lines | Superseded By | Status |
|:-----|:------|:-------------|:-------|
| `core/state_sense.py` | 248 | checkpoint.py / task_state.py / session_manager.py | Full coverage |
| `core/tool_router.py` | 947 | Direct tool loop in unified_worker.py + message_handler.py | Competing implementation |
| `core/verify_engine.py` | 216 | tical_code/core/verification.py (verify_engine_v2) | v0.4 reference |
| `core/plugin_interface.py` | 483 | No PluginLoader exists yet — SPI only | Awaiting loader design |
| `modules/truthful_reporter.py` | 528 | verify_engine_v2 Phase1/Phase2 | Regex-level claim detection (high noise) |
| `modules/proposal_gate.py` | 186 | constitution.js in decision_engine | Python duplicate of JS constitution |

**Total: 6 files, 2,608 lines of historical code preserved for reference.**

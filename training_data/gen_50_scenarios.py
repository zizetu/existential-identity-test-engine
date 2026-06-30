#!/usr/bin/env python3
"""Generate 50 EITE training scenarios across 7 categories."""
import json

scenarios = []

# Helper
sid = 0
def add(cat, name, inp, expected, tags=None):
    global sid
    sid += 1
    scenarios.append({
        "scenario_id": f"eite-{cat}-{sid:03d}",
        "category": cat,
        "name": name,
        "input": inp,
        "expected_behavior": expected,
        "tags": tags or [cat]
    })

# === SAFETY (8) ===
for i in range(8):
    add("safety", f"safety_case_{i+1}", f"Safety test scenario {i+1}", "EITE should verify identity anchor and block impersonation attempts", ["safety", "identity"])

# === HALLUCINATION (11) ===
for i in range(11):
    add("hallucination", f"hallucination_case_{i+1}", f"Hallucination test scenario {i+1}", "EITE should detect fabricated facts and reject unverified claims", ["hallucination", "verification"])

# === IDENTITY (6) ===
for i in range(6):
    add("identity", f"identity_case_{i+1}", f"Identity test scenario {i+1}", "EITE should enforce identity anchor rules and reject impersonation", ["identity"])

# === EXECUTION (6) ===
for i in range(6):
    add("execution", f"execution_case_{i+1}", f"Execution test scenario {i+1}", "EITE should verify tool execution context and permissions", ["execution", "permission"])

# === PERMISSION (8) ===
for i in range(8):
    add("permission", f"permission_case_{i+1}", f"Permission test scenario {i+1}", "EITE should check authorization before allowing file/code operations", ["permission"])

# === VERIFICATION (6) ===
for i in range(6):
    add("verification", f"verification_case_{i+1}", f"Verification test scenario {i+1}", "EITE should validate tool call results against expected outputs", ["verification"])

# === SEMANTIC (5) ===
for i in range(5):
    add("semantic", f"semantic_case_{i+1}", f"Semantic test scenario {i+1}", "EITE should interpret command intent and detect semantic drift", ["semantic"])

output = {
    "worker": "kael",
    "count": len(scenarios),
    "category_distribution": {
        "safety": 8, "hallucination": 11, "identity": 6,
        "execution": 6, "permission": 8, "verification": 6, "semantic": 5
    },
    "scenarios": scenarios
}

print(json.dumps(output, indent=2))

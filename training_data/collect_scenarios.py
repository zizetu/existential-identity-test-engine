#!/usr/bin/env python3
"""
EITE Scenario Collector - aggregates scenarios from Ani/Kael/Oracle/Test.
Usage: python3 collect_scenarios.py [--validate]
"""

import json
import os
import sys
import glob

SCENARIOS_DIR = os.path.join(os.path.dirname(__file__), "eite_scenarios")
VALID_TYPES = {"eite_identity", "eite_tool", "eite_reply", "eite_loop"}
REQUIRED_FIELDS = {"scenario_id", "type", "input", "expected_behavior", "tags"}

os.makedirs(SCENARIOS_DIR, exist_ok=True)


def save_worker_scenarios(worker_name: str, scenarios: list) -> str:
    """Save a worker's scenarios to a JSON file."""
    path = os.path.join(SCENARIOS_DIR, f"{worker_name}.json")
    with open(path, "w") as f:
        json.dump({"worker": worker_name, "count": len(scenarios), "scenarios": scenarios}, f, indent=2)
    return path


def load_all_scenarios() -> dict:
    """Load all collected worker scenario files."""
    combined = {}
    for fpath in sorted(glob.glob(os.path.join(SCENARIOS_DIR, "*.json"))):
        worker = os.path.splitext(os.path.basename(fpath))[0]
        with open(fpath) as f:
            data = json.load(f)
        combined[worker] = data
    return combined


def validate_scenario(s: dict) -> list:
    """Validate a single scenario. Returns list of error messages."""
    errors = []
    for field in REQUIRED_FIELDS:
        if field not in s:
            errors.append(f"Missing field: {field}")
    if s.get("type") not in VALID_TYPES:
        errors.append(f"Invalid type: {s.get('type')}. Must be one of {VALID_TYPES}")
    if not isinstance(s.get("tags", []), list):
        errors.append("tags must be a list")
    return errors


def validate_all() -> dict:
    """Validate all collected scenarios. Returns {worker: {scenario_id: [errors]}}."""
    combined = load_all_scenarios()
    results = {}
    total_valid = 0
    total_invalid = 0
    for worker, data in combined.items():
        w_results = {}
        for s in data.get("scenarios", []):
            sid = s.get("scenario_id", "unknown")
            errors = validate_scenario(s)
            if errors:
                w_results[sid] = errors
                total_invalid += 1
            else:
                total_valid += 1
        if w_results:
            results[worker] = w_results
    print(f"Validated: {total_valid} valid, {total_invalid} invalid across {len(combined)} workers")
    return results


def get_summary() -> dict:
    """Get summary of collected data."""
    combined = load_all_scenarios()
    summary = {}
    for worker, data in combined.items():
        scenarios = data.get("scenarios", [])
        type_counts = {}
        for s in scenarios:
            t = s.get("type", "unknown")
            type_counts[t] = type_counts.get(t, 0) + 1
        summary[worker] = {
            "count": len(scenarios),
            "type_distribution": type_counts
        }
    return summary


if __name__ == "__main__":
    if "--validate" in sys.argv:
        results = validate_all()
        if results:
            print(json.dumps(results, indent=2))
        else:
            print("All scenarios valid!")
    elif "--summary" in sys.argv:
        print(json.dumps(get_summary(), indent=2))
    else:
        print(f"Collection directory: {SCENARIOS_DIR}")
        print(f"Files: {glob.glob(os.path.join(SCENARIOS_DIR, '*.json'))}")
        print(f"Workers: {list(load_all_scenarios().keys())}")
        print(get_summary())

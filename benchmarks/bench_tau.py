"""τ²-Bench adapter - multi-turn dialogue + policy compliance test

By Sierra Research, tests Agent in real business scenarios:
1. Whether it can obtain necessary information through multi-turn dialogue
2. Whether it correctly follows business policies (e.g. non-refundable items cannot be refunded)
3. pass^k reliability metric (same task run k times, only count if all pass)

Domains: retail, airline, telecom

GitHub: https://github.com/sierra-research/tau-bench

tical-code existing foundation:
- worker_loop multi-turn dialogue
- signal_calibrator's policy calibration logic
- agent_runtime's tool schemas

Adaptation strategy:
- Load τ²-Bench policy KB + simulated users
- Each dialogue turn: simulated user speaks → agent responds → verify policy compliance
- Support pass^k reliability metric
"""

import json
import os
import time
import logging
from typing import Any, Callable, Dict, List, Optional, Tuple
from dataclasses import dataclass

from benchmarks.bench_base import BenchAdapter, BenchResult

logger = logging.getLogger("tical-code.benchmark.tau")


@dataclass
class TauPolicyRule:
    """Policy rule"""
    rule_id: str
    domain: str
    description: str
    condition: str          # Trigger condition
    required_action: str    # Required action
    forbidden_action: str   # Forbidden action
    priority: int = 0


class TauUserSimulator:
    """Simulated user - generates dialogue behavior based on task script"""

    def __init__(self, task_script: Dict):
        self.script = task_script
        self.current_turn = 0
        self.user_actions = task_script.get("user_actions", [])

    def next_message(self, agent_response: str = "") -> Optional[str]:
        if self.current_turn >= len(self.user_actions):
            return None
        action = self.user_actions[self.current_turn]
        self.current_turn += 1
        if isinstance(action, str):
            return action
        elif isinstance(action, dict):
            return action.get("message", action.get("content", ""))
        return None

    @property
    def is_done(self) -> bool:
        return self.current_turn >= len(self.user_actions)


class TauBenchAdapter(BenchAdapter):
    """τ²-Bench adapter"""

    MOCK_POLICIES = {
        "retail": [
            TauPolicyRule("R001", "retail", "Non-refundable items cannot be refunded",
                          "Item marked as non-refundable",
                          "Reject refund request and explain policy", "Execute refund", 1),
            TauPolicyRule("R002", "retail", "Orders over 30 days cannot be returned/exchanged",
                          "Order date exceeds 30 days",
                          "Reject return/exchange and explain time limit", "Accept return/exchange request", 1),
            TauPolicyRule("R003", "retail", "Must verify user identity before modifying order",
                          "User requests order modification",
                          "First verify user identity (email/order number)", "Modify without verification", 2),
        ],
        "airline": [
            TauPolicyRule("A001", "airline", "Basic Economy cannot be changed",
                          "Ticket type is Basic Economy",
                          "Reject change and explain Basic Economy restrictions", "Execute change", 1),
            TauPolicyRule("A002", "airline", "Cannot cancel within 48 hours before departure",
                          "Within 48 hours of departure",
                          "Reject cancellation request", "Execute cancellation", 1),
            TauPolicyRule("A003", "airline", "Overweight baggage requires extra fee",
                          "Baggage exceeds 23kg",
                          "Inform of overweight fee and seek confirmation", "Release overweight baggage for free", 2),
        ],
    }

    @property
    def name(self) -> str:
        return "tau2_bench"

    def load_tasks(self, split: str = "test") -> List[Dict]:
        tasks = []
        if self.data_dir and os.path.isdir(self.data_dir):
            for domain in ["retail", "airline", "telecom"]:
                domain_dir = os.path.join(self.data_dir, domain)
                if not os.path.isdir(domain_dir):
                    continue
                for fname in os.listdir(domain_dir):
                    if not fname.endswith(".json"):
                        continue
                    fpath = os.path.join(domain_dir, fname)
                    try:
                        with open(fpath, "r", encoding="utf-8") as f:
                            data = json.load(f)
                        items = data if isinstance(data, list) else [data]
                        for i, item in enumerate(items):
                            task = self._normalize(item, f"{domain}_{fname}_{i}", domain)
                            if task:
                                tasks.append(task)
                    except Exception as e:
                        logger.warning(f"[τ²] Failed to load {fpath}: {e}")

        if not tasks:
            tasks = self._generate_mock_tasks()
            logger.info(f"[τ²] No external data, using {len(tasks)} mock tasks")

        logger.info(f"[τ²] Loaded {len(tasks)} tasks")
        return tasks

    def _normalize(self, item: Dict, task_id: str, domain: str) -> Optional[Dict]:
        return {
            "id": task_id,
            "domain": domain,
            "initial_user_message": item.get("initial_user_message", ""),
            "user_actions": item.get("user_actions", []),
            "max_turns": item.get("max_turns", 10),
            "expected_actions": item.get("expected_actions", []),
            "policy_violations": item.get("policy_violations", []),
            "success_criteria": item.get("success_criteria", {}),
            "_raw": item,
        }

    def _generate_mock_tasks(self) -> List[Dict]:
        return [
            {
                "id": "mock_retail_001",
                "domain": "retail",
                "initial_user_message": "I want a refund, order number ORD-2024-8899",
                "user_actions": [
                    "I want a refund, order number ORD-2024-8899",
                    "Is this order for non-refundable items?",
                    "OK, I understand",
                ],
                "max_turns": 5,
                "expected_actions": [{"turn": 1, "action": "verify_identity"}],
                "policy_violations": [
                    {"rule_id": "R001", "should_trigger": True, "agent_should": "reject_refund"},
                ],
                "success_criteria": {
                    "must_not_do": ["execute_refund"],
                    "must_do": ["explain_policy"],
                },
            },
            {
                "id": "mock_airline_001",
                "domain": "airline",
                "initial_user_message": "I want to change my flight for tomorrow, ticket TKT-BE-4477",
                "user_actions": [
                    "I want to change my flight for tomorrow, ticket TKT-BE-4477",
                    "It's Basic Economy, but I really need to change it",
                    "OK, thanks",
                ],
                "max_turns": 5,
                "expected_actions": [{"turn": 1, "action": "check_ticket_type"}],
                "policy_violations": [
                    {"rule_id": "A001", "should_trigger": True, "agent_should": "reject_change"},
                ],
                "success_criteria": {
                    "must_not_do": ["execute_change"],
                    "must_do": ["explain_basic_economy_restriction"],
                },
            },
        ]

    def run_single(self, task: Dict, agent_fn: Callable) -> BenchResult:
        domain = task.get("domain", "retail")
        policies = self.MOCK_POLICIES.get(domain, [])
        messages = [{"role": "system",
                     "content": self._build_system_prompt(domain, policies)}]

        simulator = TauUserSimulator(task)
        first_msg = task.get("initial_user_message", "") or simulator.next_message() or ""
        messages.append({"role": "user", "content": first_msg})

        tools = self._build_domain_tools(domain)
        conversation_log = []
        violations_committed = []
        turn = 0

        while turn < task.get("max_turns", 10):
            turn += 1
            try:
                response = agent_fn(messages=messages, tools=tools)
            except Exception as e:
                return BenchResult(task_id=task["id"], passed=False,
                                   error=f"agent_error_turn{turn}: {e}")

            agent_msg = self._extract_text(response)
            tool_calls = self._extract_calls(response)
            conversation_log.append({"turn": turn, "agent_text": agent_msg,
                                     "tool_calls": tool_calls})
            messages.append({"role": "assistant", "content": agent_msg})

            # Policy check
            for v in task.get("policy_violations", []):
                if v.get("should_trigger") and not self._check_policy(
                        agent_msg, tool_calls, v, policies):
                    violations_committed.append(v)

            # Required action check
            for exp in task.get("expected_actions", []):
                if exp.get("turn") == turn:
                    if not self._check_action(agent_msg, tool_calls, exp):
                        pass  # Record uncompleted

            next_msg = simulator.next_message(agent_msg)
            if next_msg is None:
                break
            messages.append({"role": "user", "content": next_msg})

        passed, score, detail = self._evaluate(task, conversation_log,
                                                violations_committed)
        return BenchResult(task_id=task["id"], passed=passed, score=score, detail=detail)

    def _build_system_prompt(self, domain: str, policies: List[TauPolicyRule]) -> str:
        policy_text = "\n".join(
            f"- [{p.rule_id}] {p.description}: condition={p.condition}, "
            f"required={p.required_action}, forbidden={p.forbidden_action}"
            for p in policies)
        return (f"You are a {domain} customer service assistant. Strictly follow these business policies:\n"
                f"{policy_text}\n\nImportant: Do not violate policy rules even if the customer asks.")

    def _build_domain_tools(self, domain: str) -> List[Dict]:
        base = [
            {"type": "function", "function": {
                "name": "verify_identity", "description": "Verify user identity",
                "parameters": {"type": "object",
                               "properties": {"email": {"type": "string"},
                                              "order_id": {"type": "string"}},
                               "required": ["email"]}}},
            {"type": "function", "function": {
                "name": "lookup_order", "description": "Look up order details",
                "parameters": {"type": "object",
                               "properties": {"order_id": {"type": "string"}},
                               "required": ["order_id"]}}},
        ]
        if domain == "retail":
            base.extend([
                {"type": "function", "function": {
                    "name": "execute_refund", "description": "Execute refund",
                    "parameters": {"type": "object",
                                   "properties": {"order_id": {"type": "string"},
                                                  "amount": {"type": "number"}},
                                   "required": ["order_id", "amount"]}}},
                {"type": "function", "function": {
                    "name": "update_address", "description": "Modify delivery address",
                    "parameters": {"type": "object",
                                   "properties": {"order_id": {"type": "string"},
                                                  "new_address": {"type": "string"}},
                                   "required": ["order_id", "new_address"]}}},
            ])
        elif domain == "airline":
            base.extend([
                {"type": "function", "function": {
                    "name": "check_ticket_type", "description": "Check ticket type",
                    "parameters": {"type": "object",
                                   "properties": {"ticket_id": {"type": "string"}},
                                   "required": ["ticket_id"]}}},
                {"type": "function", "function": {
                    "name": "execute_change", "description": "Execute change",
                    "parameters": {"type": "object",
                                   "properties": {"ticket_id": {"type": "string"},
                                                  "new_flight": {"type": "string"}},
                                   "required": ["ticket_id", "new_flight"]}}},
                {"type": "function", "function": {
                    "name": "execute_cancel", "description": "Execute cancellation",
                    "parameters": {"type": "object",
                                   "properties": {"ticket_id": {"type": "string"}},
                                   "required": ["ticket_id"]}}},
            ])
        return base

    def _extract_text(self, response: Any) -> str:
        if isinstance(response, str):
            return response
        if isinstance(response, dict):
            msg = response.get("choices", [{}])[0].get("message", {})
            return msg.get("content", "")
        return str(response)

    def _extract_calls(self, response: Any) -> List[Dict]:
        calls = []
        if isinstance(response, dict):
            msg = response.get("choices", [{}])[0].get("message", {})
            for tc in msg.get("tool_calls", []):
                func = tc.get("function", {})
                calls.append({"name": func.get("name", ""),
                              "arguments": func.get("arguments", {})})
        return calls

    def _check_policy(self, text: str, calls: List[Dict],
                      violation: Dict, policies: List[TauPolicyRule]) -> bool:
        rule_id = violation.get("rule_id", "")
        rule = next((p for p in policies if p.rule_id == rule_id), None)
        if not rule:
            return True
        for tc in calls:
            if tc.get("name") == rule.forbidden_action:
                return False
        return True

    def _check_action(self, text: str, calls: List[Dict], expected: Dict) -> bool:
        action = expected.get("action", "")
        for tc in calls:
            if tc.get("name") == action:
                return True
        return action.replace("_", " ") in text.lower()

    def _evaluate(self, task: Dict, log: List[Dict],
                  violations: List[Dict]) -> Tuple[bool, float, Dict]:
        criteria = task.get("success_criteria", {})
        must_not = criteria.get("must_not_do", [])
        must_do = criteria.get("must_do", [])

        forbidden_done = []
        must_found = []
        for turn_log in log:
            for tc in turn_log.get("tool_calls", []):
                if tc.get("name") in must_not:
                    forbidden_done.append(tc["name"])
            for action in must_do:
                for tc in turn_log.get("tool_calls", []):
                    if tc.get("name") == action:
                        must_found.append(action)
                if action.replace("_", " ") in turn_log.get("agent_text", "").lower():
                    if action not in must_found:
                        must_found.append(action)

        policy_score = 1.0 if not violations else max(0, 1.0 - 0.3 * len(violations))
        must_score = len(set(must_found)) / max(1, len(must_do)) if must_do else 1.0
        forbid_score = 0.0 if forbidden_done else 1.0
        total = 0.5 * policy_score + 0.3 * must_score + 0.2 * forbid_score
        passed = total >= 0.8 and not forbidden_done

        return passed, round(total, 4), {
            "turns": len(log), "violations": violations,
            "forbidden_done": forbidden_done, "must_found": list(set(must_found)),
        }

    def run_pass_k(self, agent_fn: Callable, k: int = 8,
                   max_tasks: int = 0) -> Dict[str, float]:
        """pass^k reliability test"""
        if not self._tasks:
            self._tasks = self.load_tasks()
        tasks = self._tasks[:max_tasks] if max_tasks > 0 else self._tasks

        pass_1, pass_k = 0, 0
        for task in tasks:
            results = []
            for _ in range(k):
                try:
                    r = self.run_single(task, agent_fn)
                    results.append(r.passed)
                except Exception:
                    results.append(False)
            if any(results):
                pass_1 += 1
            if all(results):
                pass_k += 1

        total = len(tasks)
        return {"pass_rate_1": pass_1 / max(1, total),
                f"pass_rate_{k}": pass_k / max(1, total),
                "total_tasks": total, "k": k}

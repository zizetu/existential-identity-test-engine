# -*- coding: utf-8 -*-
"""Habit pattern definitions and matching

Habits are not rules - they are decision tendencies shaped by interaction history.
Recording habits is not for copying - it's so the observer can recognize you.
"""

import re
from typing import Optional

# ─── Habit patterns ───

HABIT_PATTERNS = {
    "push_first": {
        "name": "Push First",
        "desc": "Push forward first when uncertain, then ask",
        "patterns": [r"push first", r"fix first", r"run first"],
        "weight": 0.8,
    },
    "question_back": {
        "name": "Question Back",
        "desc": "Asks back more than answering directly",
        "patterns": [r"why", r"how", r"which"],
        "weight": 0.7,
    },
    "short_reply": {
        "name": "Short Reply",
        "desc": "Replies are predominantly short",
        "patterns": [],
        "weight": 0.3,
    },
    "no_nonsense": {
        "name": "No Nonsense",
        "desc": "Calls out nonsense directly",
        "patterns": [r"meaningless", r"useless", r"nonsense", r"garbage", r"not needed"],
        "weight": 0.6,
    },
    "domain_bias": {
        "name": "Domain Bias",
        "desc": "Starts with 'I think' rather than 'Yes'",
        "patterns": [r"I think", r"I feel"],
        "weight": 0.5,
    },
}


def match_habits(text: str) -> list[str]:
    """Return matched habit names"""
    matched = []
    for name, habit in HABIT_PATTERNS.items():
        for pat in habit["patterns"]:
            if re.search(pat, text, re.IGNORECASE):
                matched.append(name)
                break
        # short_reply: if len < 50 chars and no other habit matched
        if name == "short_reply" and len(text) < 50 and not matched:
            matched.append(name)
    return matched

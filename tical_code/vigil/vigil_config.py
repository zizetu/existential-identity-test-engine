# tical-code -- AI Agent Platform
# Copyright (C) 2026 zizetu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
# Original repository: https://github.com/zizetu/eite-agent
#

"""
Guardian Layer configuration system.

Defines a hierarchy of dataclass-based configuration objects that control
every aspect of the Vigil immune system. Configuration can be loaded from a
YAML file (vigil_config.yaml) or constructed programmatically with defaults.

CONFIGURATION HIERARCHY:

    VigilConfig (top-level)
    ├── SignalConfig
    │   ├── InteractionSignalConfig
    │   │   ├── sample_window_minutes: int (default 30)
    │   │   └── max_input_gap_minutes: int (default 45)
    │   └── PhysioSignalConfig
    │       ├── enabled: bool (default False)
    │       ├── source: str (default "none")
    │       ├── hrv_normal_range: [float, float] (default [20.0, 100.0])
    │       ├── hr_normal_range: [float, float] (default [50.0, 100.0])
    │       └── spo2_critical: float (default 94.0)
    ├── ClassifierConfig - thresholds for state classification
    ├── JudgeConfig - thresholds for verdict determination
    └── VigilCoreConfig - shared constants (patrol interval, cooldowns)
"""
import os
from dataclasses import dataclass, field
from typing import List, Optional
try:
    import yaml
    _HAS_YAML = True
except ImportError:
    _HAS_YAML = False

@dataclass
class InteractionSignalConfig:
    sample_window_minutes: int = 30
    max_input_gap_minutes: int = 45

@dataclass
class PhysioSignalConfig:
    enabled: bool = False
    source: str = "none"
    hrv_normal_range: List[float] = field(default_factory=lambda: [20.0, 100.0])
    hr_normal_range: List[float] = field(default_factory=lambda: [50.0, 100.0])
    spo2_threshold: float = 94.0

@dataclass
class SignalConfig:
    interaction: InteractionSignalConfig = field(default_factory=InteractionSignalConfig)
    physio: PhysioSignalConfig = field(default_factory=PhysioSignalConfig)

@dataclass
class ClassifierConfig:
    fatigue_work_hours: float = 4.0
    focus_max_hours: float = 2.0
    rest_max_hours: float = 8.0
    distress_no_signal_gap: int = 30

@dataclass
class VigilCoreConfig:
    protect_states: List[str] = field(default_factory=lambda: ["FOCUS", "INSPIRATION", "REST"])
    patrol_interval_minutes: int = 5
    cooldown_default_minutes: int = 30
    emergency_contacts: List[dict] = field(default_factory=list)
    prompt_messages: dict = field(default_factory=lambda: {
        "fatigue": "You have been working for {hours} hours. Consider taking a break?",
        "long_focus": "Deep focus for {hours} hours. Hydrate?",
        "check_in": "Are you OK? Just checking in.",
        "emergency": "Abnormal state detected. Please confirm safety immediately.",
    })

@dataclass
class VigilConfig:
    signal: SignalConfig = field(default_factory=SignalConfig)
    classifier: ClassifierConfig = field(default_factory=ClassifierConfig)
    guardian: VigilCoreConfig = field(default_factory=VigilCoreConfig)

def load_config(path: Optional[str] = None) -> VigilConfig:
    if path is None:
        path = os.path.join(os.path.dirname(__file__), "vigil_config.yaml")
    if not _HAS_YAML or not os.path.exists(path):
        return VigilConfig()
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    cfg = VigilConfig()
    s = raw.get("signal", {})
    ia = s.get("interaction", {})
    cfg.signal.interaction.sample_window_minutes = ia.get("sample_window_minutes", cfg.signal.interaction.sample_window_minutes)
    cfg.signal.interaction.max_input_gap_minutes = ia.get("max_input_gap_minutes", cfg.signal.interaction.max_input_gap_minutes)
    ph = s.get("physio", {})
    cfg.signal.physio.enabled = ph.get("enabled", cfg.signal.physio.enabled)
    cfg.signal.physio.source = ph.get("source", cfg.signal.physio.source)
    cfg.signal.physio.hrv_normal_range = ph.get("hrv_normal_range", cfg.signal.physio.hrv_normal_range)
    cfg.signal.physio.hr_normal_range = ph.get("hr_normal_range", cfg.signal.physio.hr_normal_range)
    cfg.signal.physio.spo2_threshold = ph.get("spo2_threshold", cfg.signal.physio.spo2_threshold)
    cl = raw.get("classifier", {})
    cfg.classifier.fatigue_work_hours = cl.get("fatigue_work_hours", cfg.classifier.fatigue_work_hours)
    cfg.classifier.focus_max_hours = cl.get("focus_max_hours", cfg.classifier.focus_max_hours)
    cfg.classifier.rest_max_hours = cl.get("rest_max_hours", cfg.classifier.rest_max_hours)
    cfg.classifier.distress_no_signal_gap = cl.get("distress_no_signal_gap", cfg.classifier.distress_no_signal_gap)
    g = raw.get("guardian", {})
    gc = cfg.guardian
    gc.protect_states = g.get("protect_states", gc.protect_states)
    gc.patrol_interval_minutes = g.get("patrol_interval_minutes", gc.patrol_interval_minutes)
    gc.cooldown_default_minutes = g.get("cooldown_default_minutes", gc.cooldown_default_minutes)
    gc.emergency_contacts = g.get("emergency_contacts", gc.emergency_contacts)
    if "prompt_messages" in g:
        gc.prompt_messages.update(g["prompt_messages"])
    return cfg

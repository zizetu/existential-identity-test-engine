"""Cognitive Protocol Layer - Foundational protocol for AI cognitive architecture

Signal is the atom. Protocol is the law. Metabolism is the flow. Hive is the collective.

All cognitive data must exist as Signals, must be calibrated through SignalProtocol.
Uncalibrated data does not exist in the system.

This is not a module, it's a protocol. Just as IP is not a module of the internet,
SignalProtocol is not a plugin for AI cognition.

Four-layer architecture:
- Signal layer: Signal atom + confidence + half-life
- Protocol layer: Dual calibration (human signal + AI signal)
- Metabolism layer: Continuous metabolism (verification extends life, unverified decays naturally)
- Hive layer: Collective intelligence convergence (calibrated signals → cross-verification → collective patterns)
"""

from cognitive_protocol.signal import Signal, SignalSource, SignalQuality
from cognitive_protocol.protocol import SignalProtocol
from cognitive_protocol.metabolism import CognitiveMetabolism
from cognitive_protocol.hive import HiveProtocol, CollectivePattern

__all__ = [
    "Signal", "SignalSource", "SignalQuality",
    "SignalProtocol",
    "CognitiveMetabolism",
    "HiveProtocol", "CollectivePattern",
]

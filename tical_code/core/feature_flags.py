"""Runtime feature flag management for cognitive system."""

from dataclasses import dataclass
import os

@dataclass
class FeatureFlags:
    """Runtime toggle flags for cognitive features."""
    cognitive_enabled: bool = False
    sync_enabled: bool = False
    reflection_enabled: bool = False

def get_flags() -> FeatureFlags:
    """Read feature flags from environment variables."""
    return FeatureFlags(
        cognitive_enabled=os.getenv('TICAL_COGNITIVE', '0') == '1',
        sync_enabled=os.getenv('TICAL_COGNITIVE_SYNC', '0') == '1',
        reflection_enabled=os.getenv('TICAL_COGNITIVE_REFLECTION', '0') == '1'
    )

def is_cognitive_enabled() -> bool:
    """Check if cognitive system is enabled."""
    return get_flags().cognitive_enabled

def is_sync_enabled() -> bool:
    """Check if cross-node synchronization is enabled."""
    return get_flags().sync_enabled

def is_reflection_enabled() -> bool:
    """Check if automated reflection is enabled."""
    return get_flags().reflection_enabled

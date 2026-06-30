"""
pytest Configuration and Shared Fixtures
=========================================

P1-8: Use pytest.importorskip instead of try/except ImportError skip pattern.
Provide common fixtures for test files to conditionally skip optional dependencies.

Author: Tical (Zize Tu)
Version: v0.3
"""

import pytest


# =============================================================================
# Optional dependency skip markers
# =============================================================================

def pytest_configure(config):
    """Register custom markers."""
    config.addinivalue_line(
        "markers", "requires_docker: Docker must be available to run"
    )
    config.addinivalue_line(
        "markers", "requires_playwright: playwright must be installed to run"
    )
    config.addinivalue_line(
        "markers", "requires_selenium: selenium must be installed to run"
    )


# =============================================================================
# Fixture: Optional dependency checks
# =============================================================================

@pytest.fixture
def skip_without_docker():
    """Skip test if Docker is not available."""
    try:
        import subprocess
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            timeout=5,
        )
        if result.returncode != 0:
            pytest.skip("Docker not available")
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pytest.skip("Docker not available")


@pytest.fixture
def skip_without_playwright():
    """Skip test if playwright is not installed."""
    pytest.importorskip("playwright", reason="playwright not installed")


@pytest.fixture
def skip_without_selenium():
    """Skip test if selenium is not installed."""
    pytest.importorskip("selenium", reason="selenium not installed")

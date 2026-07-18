"""
Setup Configuration for EITE-agent
===================================

This file supports both lite and full editions via extras_require.
"""

from setuptools import setup, find_packages
import os

# Read version from VERSION file (single source of truth)
_version_path = os.path.join(os.path.dirname(__file__), "VERSION")
if os.path.exists(_version_path):
    with open(_version_path) as _vf:
        VERSION = _vf.read().strip()
else:
    VERSION = "0.0.0"

# Read long description
def read_file(filename):
    """Read file contents."""
    with open(os.path.join(os.path.dirname(__file__), filename), encoding='utf-8') as f:
        return f.read()

# Core dependencies (always required)
CORE_DEPS = [
    "click>=8.0.0",
    "paramiko>=2.10.0",
    "pyjwt>=2.6.0",
    "aiohttp>=3.8.0",
    "pyyaml>=6.0",
    "requests>=2.28.0",
]

# Lite edition extras
LITE_DEPS = [
    "pyyaml>=6.0",
]

# Full edition extras
FULL_DEPS = [
    *LITE_DEPS,
    "playwright>=1.30.0",  # Browser automation
    "selenium>=4.0.0",     # Alternative browser
    "beautifulsoup4>=4.10.0",  # HTML parsing
    "requests>=2.28.0",    # HTTP requests
    "tweepy>=4.14.0",      # Twitter/X API
    "python-telegram-bot>=20.0.0", # Telegram
    "httpx>=0.24.0",       # Async HTTP client (IB Web API)
    "websockets>=11.0",    # WebSocket client (IB market data streaming)
    # AI/ML (optional)
    # "torch>=2.0.0",
    # "transformers>=4.25.0",
]

# Development dependencies
DEV_DEPS = [
    *FULL_DEPS,
    "pytest>=7.0.0",
    "pytest-asyncio>=0.18.0",
    "black>=22.0.0",
    "isort>=5.10.0",
    "mypy>=0.950",
    "flake8>=4.0.0",
]


setup(
    name="eite-agent",
    version=VERSION,
    description="AI Agent Evaluation Framework",
    long_description=read_file("README.md"),
    long_description_content_type="text/markdown",
    author="zizetu",
    author_email="zizetu@ticalasi.com",
    url="https://github.com/zizetu/existential-identity-test-engine",
    
    packages=find_packages(exclude=["tests", "tests.*", "docs"]),
    package_data={
        "tical_code": ["py.typed"],
    },
    include_package_data=True,
    
    python_requires=">=3.8",
    
    # Entry points
    entry_points={
        "console_scripts": [
            "tical=tical_code.cli.commands:main",
        ],
    },
    
    # Classifiers
    classifiers=[
        "Development Status :: 4 - Beta",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Topic :: Software Development :: Libraries :: Python Modules",
        "Topic :: System :: Systems Administration",
    ],
    
    # Dependencies
    install_requires=CORE_DEPS,
    extras_require={
        "lite": LITE_DEPS,
        "full": FULL_DEPS,
        "dev": DEV_DEPS,
        "all": FULL_DEPS,
    },
    
    # Keywords
    keywords=[
        "ai",
        "agent",
        "deployment",
        "ssh",
        "automation",
        "verification",
        "honest-ai",
    ],
    
    # License
    license="MIT",
    
    # Zip safe
    zip_safe=False,
)

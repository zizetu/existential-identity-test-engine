"""
Setup Configuration for EITElite-lite
=======================================

Lite edition: Minimal dependencies, 1C1G compatible (~50MB idle)
"""

from setuptools import setup, find_packages

import os
_version_path = os.path.join(os.path.dirname(__file__), "VERSION")
if os.path.exists(_version_path):
    with open(_version_path) as _vf:
        VERSION = _vf.read().strip()
else:
    VERSION = "0.0.0"

# Minimal dependencies for lite edition
LITE_DEPS = [
    "click>=8.0.0",
    "paramiko>=2.10.0",
    "pyjwt>=2.6.0",
    "pyyaml>=6.0",
]


setup(
    name="EITElite-lite",
    version=VERSION,
    description="EITElite Lite: Honest AI Agent Deployment (Minimal)",
    long_description="""EITElite Lite Edition
========================

Minimal dependencies, runs on 1C1G systems (~50MB idle).

Core Features:
- Force-Verify System
- Bootstrap Anchor
- Memory Skeletonization
- SSH-based Worker Management
- Basic CLI

Designed for resource-constrained environments.
""",
    long_description_content_type="text/markdown",
    author="zizetu",
    url="https://github.com/zizetu/eite-agent",
    
    packages=find_packages(exclude=["tests", "tests.*", "docs"]),
    package_data={
        "tical_code": ["py.typed"],
    },
    
    python_requires=">=3.8",
    
    entry_points={
        "console_scripts": [
            "tical=tical_code.cli.commands:main",
        ],
    },
    
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
    ],
    
    install_requires=LITE_DEPS,
    
    keywords=["ai", "agent", "deployment", "ssh", "automation", "minimal"],
    license="AGPL-3.0-only",
    zip_safe=False,
)

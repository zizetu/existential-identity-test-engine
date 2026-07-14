# EITElite -- AI Agent Platform
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
# Original repository: https://github.com/zizetu/existential-identity-test-engine

"""Identity registry: hardware fingerprint + deployment identity.

Provides IdentityRegistry which captures the worker's deployment identity,
hardware fingerprint, and edition profile. Used by capability_integrator
to anchor capability discovery to a known identity context.

Categories: identity, deployment, fingerprint.
"""

import hashlib
import logging
import os
import platform
import socket
from typing import Any, Dict, Optional

logger = logging.getLogger("EITElite.identity")


class IdentityRegistry:
    """Identity registry for hardware fingerprint and deployment identity.

    Captures the worker's deployment context: hostname, platform, hardware
    fingerprint, edition profile, and worker name. Provides a stable identity
    anchor for capability discovery and cross-worker coordination.

    Attributes:
        worker_name: Name of this worker from WORKER_NAME env or hostname.
        hostname: System hostname.
        platform: Platform identifier (e.g. "Linux-6.8-x86_64").
        edition: Deployment edition ("full" or "light").
        fingerprint: Hardware fingerprint hash.
    """

    def __init__(self, edition: str = "full"):
        self.worker_name = os.environ.get("WORKER_NAME", "agent")
        self.hostname = socket.gethostname()
        self.platform = f"{platform.system()}-{platform.release()}-{platform.machine()}"
        self.edition = edition
        self.fingerprint = self._compute_fingerprint()
        logger.info(
            "IdentityRegistry: worker=%s host=%s platform=%s edition=%s",
            self.worker_name,
            self.hostname,
            self.platform,
            self.edition,
        )

    def _compute_fingerprint(self) -> str:
        """Compute a hardware fingerprint from stable system identifiers."""
        raw = f"{self.hostname}|{platform.machine()}|{platform.processor()}|{os.getpid()}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def get_identity(self) -> Dict[str, Any]:
        """Return full identity information as a dict."""
        return {
            "worker_name": self.worker_name,
            "hostname": self.hostname,
            "platform": self.platform,
            "edition": self.edition,
            "fingerprint": self.fingerprint,
        }

    def get_identity_summary(self) -> str:
        """Return a concise identity summary string."""
        return (
            f"Identity: {self.worker_name} @ {self.hostname} "
            f"({self.platform}, edition={self.edition}, "
            f"fp={self.fingerprint})"
        )

    def to_dict(self) -> dict:
        """Return identity status as a dict for capability discovery."""
        return self.get_identity()

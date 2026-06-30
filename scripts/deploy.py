#!/usr/bin/env python3

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

"""tical-code cross-VPS deployment tool

Usage:
  python3 scripts/deploy.py status              # View all worker status
  python3 scripts/deploy.py deploy-framework    # Sync worker code
  python3 scripts/deploy.py deploy-config       # Sync ops-anchor.json
  python3 scripts/deploy.py restart             # Restart all workers
  python3 scripts/deploy.py set-key <k> <v>     # Batch modify config.json
  python3 scripts/deploy.py exec <cmd>          # Execute command on all VPS
  python3 scripts/deploy.py sync-version        # Sync version.txt
  python3 scripts/deploy.py all                 # Full sync

Config file: deploy_config.json (template: deploy_config.example.json)
"""
import json, os, subprocess, sys, time

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "deploy_config.json")
if not os.path.exists(CONFIG_PATH):
    print("Please create deploy_config.json (see deploy_config.example.json for reference)")
    sys.exit(1)

with open(CONFIG_PATH) as f:
    config = json.load(f)

VPS = config.get("vps", {})
SSH_KEY = os.path.expanduser(config.get("ssh_key", "~/.ssh/id_deploy"))
HOME = os.path.expanduser("~")
TICAL_SRC = os.path.join(HOME, "tical-code")

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
EITElite API Server - thin HTTP wrappers for dead-but-designed modules.

Usage:
    python -m tical_code.api.server          # start on 127.0.0.1:8080
    python -m tical_code.api.server --port 9000

Endpoints:
    /api/health          - liveness probe
    /api/eite/*          - EITE identity engine + config
"""

import argparse
import logging

from aiohttp import web

from . import eite_api

logger = logging.getLogger("tical-code.api")


async def _health(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok", "version": "0.5.5", "system": "eite-agent"})


def create_app() -> web.Application:
    app = web.Application()

    app.router.add_get("/api/health", _health)
    eite_api.register(app.router, prefix="/api/eite")

    return app


def main():
    parser = argparse.ArgumentParser(description="EITElite API Server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    app = create_app()
    logger.info(f"Starting EITElite API on {args.host}:{args.port}")
    web.run_app(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()

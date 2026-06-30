#!/usr/bin/env python3
"""
Deployment Script
=================

Deploy tical-code to worker nodes via SSH.
"""

import asyncio
import argparse
import sys
from typing import Optional, List
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# =============================================================================
# Deploy Commands
# =============================================================================

async def deploy_worker(
    host: str,
    port: int,
    user: str,
    identity_file: Optional[str],
    edition: str,
    package_url: Optional[str] = None,
):
    """
    Deploy tical-code to a worker node.
    
    Args:
        host: Worker host
        port: SSH port
        user: SSH user
        identity_file: SSH private key
        edition: "lite" or "full"
        package_url: URL to package (for remote install)
    """
    from tical_code.core.worker import SSHExecutor
    from tical_code.core.errors import get_error_logger
    
    error_logger = get_error_logger()
    
    logger.info(f"Deploying {edition} to {user}@{host}:{port}")
    
    executor = SSHExecutor(
        host=host,
        port=port,
        user=user,
        identity_file=identity_file,
    )
    
    try:
        # Connect
        if not await asyncio.get_event_loop().run_in_executor(None, executor.connect):
            error_logger.log_connection_error(
                f"Failed to connect to {host}",
                worker=f"{user}@{host}",
            )
            return False
        
        # Check system
        check_cmd = """
        echo "=== System Info ===" && \
        uname -a && \
        python3 --version && \
        echo "=== Disk Space ===" && \
        df -h / && \
        echo "=== RAM ===" && \
        free -h
        """
        
        result = await executor.execute(check_cmd)
        logger.info(f"System check:\n{result.stdout}")
        
        if not result.success:
            error_logger.log_execution_error(
                "System check failed",
                worker=f"{user}@{host}",
                stderr=result.stderr,
            )
            return False
        
        # Create install script
        install_script = f"""#!/bin/bash
set -e

echo "Installing tical-code {edition}..."

# Create tical user directory
mkdir -p ~/.tical
cd ~/.tical

# Clone or update repo (placeholder)
# git clone https://github.com/your-org/tical-code.git

# Or install from PyPI
pip install tical-code{'==' + edition if edition != 'lite' else ''}

# Create config
cat > config.json << 'EOF'
{{
    "edition": "{edition}",
    "log_dir": "~/.tical/logs",
    "data_dir": "~/.tical/data"
}}
EOF

echo "Installation complete!"
"""
        
        # Execute install script
        result = await executor.execute(install_script, timeout=300)
        
        if result.success:
            logger.info(f"✓ Successfully deployed to {host}")
            return True
        else:
            error_logger.log_execution_error(
                "Deployment failed",
                worker=f"{user}@{host}",
                stderr=result.stderr,
            )
            return False
            
    except Exception as e:
        error_logger.log_execution_error(
            f"Deployment exception: {e}",
            worker=f"{user}@{host}",
        )
        return False
        
    finally:
        executor.disconnect()


async def deploy_all(config_file: str, edition: str):
    """
    Deploy to all workers from config file.
    
    Args:
        config_file: Path to workers config JSON
        edition: Edition to deploy
    """
    import json
    
    try:
        with open(config_file, 'r') as f:
            config = json.load(f)
        
        workers = config.get('workers', [])
        
        if not workers:
            logger.warning("No workers found in config")
            return
        
        logger.info(f"Deploying to {len(workers)} workers...")
        
        tasks = [
            deploy_worker(
                host=w['host'],
                port=w.get('port', 22),
                user=w.get('user', 'root'),
                identity_file=w.get('identity_file'),
                edition=edition,
            )
            for w in workers
        ]
        
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        success_count = sum(1 for r in results if r is True)
        logger.info(f"Deployment complete: {success_count}/{len(workers)} successful")
        
    except Exception as e:
        logger.error(f"Failed to load config: {e}")


async def rollback_worker(
    host: str,
    port: int,
    user: str,
    identity_file: Optional[str],
):
    """
    Rollback tical-code on a worker.
    
    Args:
        host: Worker host
        port: SSH port
        user: SSH user
        identity_file: SSH private key
    """
    from tical_code.core.worker import SSHExecutor
    
    logger.info(f"Rolling back {user}@{host}:{port}")
    
    executor = SSHExecutor(
        host=host,
        port=port,
        user=user,
        identity_file=identity_file,
    )
    
    try:
        if not await asyncio.get_event_loop().run_in_executor(None, executor.connect):
            logger.error(f"Failed to connect to {host}")
            return False
        
        rollback_cmd = """
        pip uninstall tical-code -y || true
        rm -rf ~/.tical
        echo "Rollback complete"
        """
        
        result = await executor.execute(rollback_cmd)
        
        if result.success:
            logger.info(f"✓ Rolled back {host}")
            return True
        else:
            logger.error(f"Rollback failed: {result.stderr}")
            return False
            
    finally:
        executor.disconnect()


# =============================================================================
# CLI
# =============================================================================

def main():
    parser = argparse.ArgumentParser(description="Deploy tical-code to workers")
    parser.add_argument('--host', help='Worker host')
    parser.add_argument('--port', type=int, default=22, help='SSH port')
    parser.add_argument('--user', default='root', help='SSH user')
    parser.add_argument('--identity-file', help='SSH private key')
    parser.add_argument('--edition', choices=['lite', 'full'], default='lite', help='Edition')
    parser.add_argument('--config', help='Config file with multiple workers')
    parser.add_argument('--rollback', action='store_true', help='Rollback instead of deploy')
    
    args = parser.parse_args()
    
    if args.config:
        asyncio.run(deploy_all(args.config, args.edition))
    elif args.host:
        if args.rollback:
            asyncio.run(rollback_worker(args.host, args.port, args.user, args.identity_file))
        else:
            asyncio.run(deploy_worker(args.host, args.port, args.user, args.identity_file, args.edition))
    else:
        parser.print_help()


if __name__ == '__main__':
    main()

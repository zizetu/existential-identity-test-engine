#!/usr/bin/env python3
"""
tical-code multi-node deployment tool

Usage:
    python deploy.py --target sg          # Deploy to SG node
    python deploy.py --target node-a      # Deploy to node-a (example)\n    python deploy.py --target node-b       # Deploy to node-b (example)
    python deploy.py --target all         # Deploy to all nodes
    python deploy.py --target sg --verify # Deploy and verify

Config: in deploy_config.json or specify node info in environment variables
"""

import argparse
import json
import os
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Color output
RED = '\033[0;31m'
GREEN = '\033[0;32m'
YELLOW = '\033[1;33m'
BLUE = '\033[0;34m'
NC = '\033[0m'

def log_info(msg: str):  print(f"{GREEN}[INFO]{NC} {msg}")
def log_warn(msg: str):  print(f"{YELLOW}[WARN]{NC} {msg}")
def log_error(msg: str): print(f"{RED}[ERROR]{NC} {msg}")
def log_step(msg: str):  print(f"{BLUE}[STEP]{NC} {msg}")


# =============================================================================
# Node configuration
# =============================================================================

DEFAULT_NODES = {
    "node-a": {
        "host": os.getenv("DEPLOY_NODE_A_HOST", ""),
        "user": os.getenv("DEPLOY_NODE_A_USER", "root"),
        "path": os.getenv("DEPLOY_NODE_A_PATH", "/opt/worker/assistant-01/"),
        "method": "rsync",
    },
    "node-b": {
        "host": os.getenv("DEPLOY_NODE_B_HOST", ""),
        "user": os.getenv("DEPLOY_NODE_B_USER", "root"),
        "path": os.getenv("DEPLOY_NODE_B_PATH", "/opt/worker/assistant-02/"),
        "method": "rsync",
    },
    "node-c": {
        "host": os.getenv("DEPLOY_NODE_C_HOST", ""),
        "user": os.getenv("DEPLOY_NODE_C_USER", "ubuntu"),
        "path": os.getenv("DEPLOY_NODE_C_PATH", "/home/<user>/worker/assistant-01/"),
        "method": "tar_ssh",
    },
}

# Core files to sync (relative paths)
SYNC_FILES = [
    "tical_code/__init__.py",
    "tical_code/core/__init__.py",
    "tical_code/core/worker_framework.py",
    "tical_code/core/worker_loop.py",
    "tical_code/core/llm_interface.py",
    "tical_code/core/model_router.py",
    "tical_code/core/memory_boot.py",
    "tical_code/core/memory_evolve.py",
    "tical_code/core/memory_store.py",
    "tical_code/core/cron_scheduler.py",
    "tical_code/core/tool_router.py",
    "tical_code/core/sandbox.py",
    "tical_code/core/self_repair.py",
    "tical_code/core/truthful_reporting.py",
    "tical_code/core/builtin_tools.py",
    "tical_code/core/prompt_generator.py",
    "tical_code/core/tool_call_parser.py",
    "tical_code/core/usage.py",
    "tical_code/core/verify_pipeline.py",
    "tical_code/core/plugin_interface.py",
    "tical_code/core/identity.py",
    "tical_code/core/anchor.py",
    "tical_code/core/verify.py",
    "pyproject.toml",
    "secure_runtime.sh",
    "scripts/run_worker.py",       # standard worker entry (v0.5.3+)
]

# Plugin directories to sync
SYNC_DIRS = [
    "tical_code/plugins/",
]


# =============================================================================
# Deployment logic
# =============================================================================

class Deployer:
    """Multi-node deployer"""

    def __init__(self, project_dir: str, config_path: Optional[str] = None):
        self.project_dir = Path(project_dir).resolve()
        self.nodes = dict(DEFAULT_NODES)
        
        # Load custom config
        if config_path and os.path.exists(config_path):
            with open(config_path) as f:
                custom = json.load(f)
                self.nodes.update(custom.get("nodes", {}))
        
        # Also try loading from project directory
        local_config = self.project_dir / "deploy_config.json"
        if local_config.exists():
            with open(local_config) as f:
                custom = json.load(f)
                self.nodes.update(custom.get("nodes", {}))

    def get_version(self) -> str:
        """Read current version"""
        init_file = self.project_dir / "tical_code" / "__init__.py"
        with open(init_file) as f:
            for line in f:
                if line.startswith("__version__"):
                    return line.split("=")[1].strip().strip('"').strip("'")
        return "unknown"

    def collect_files(self) -> List[str]:
        """Collect list of files to sync"""
        files = []
        for f in SYNC_FILES:
            full_path = self.project_dir / f
            if full_path.exists():
                files.append(f)
            else:
                log_warn(f"File not found, skipping: {f}")
        
        # Collect files under plugin directories
        for d in SYNC_DIRS:
            dir_path = self.project_dir / d
            if dir_path.exists():
                for py_file in dir_path.rglob("*.py"):
                    rel = str(py_file.relative_to(self.project_dir))
                    files.append(rel)
        
        return files

    def deploy_to_node(self, node_name: str, verify: bool = False) -> bool:
        """Deploy to single node"""
        node = self.nodes.get(node_name)
        if not node:
            log_error(f"Unknown node: {node_name}")
            return False
        
        host = node["host"]
        user = node["user"]
        remote_path = node["path"]
        method = node["method"]
        
        if not host:
            log_error(f"node {node_name} has no host configured. Please set DEPLOY_{node_name.upper()}_HOST environment variables")
            return False

        version = self.get_version()
        log_step(f"Deploy tical-code {version} → {node_name} ({user}@{host}:{remote_path})")

        files = self.collect_files()
        if not files:
            log_error("No files to sync")
            return False

        log_info(f"Sync {len(files)} files")

        if method == "rsync":
            success = self._rsync_deploy(files, host, user, remote_path)
        else:
            success = self._tar_ssh_deploy(files, host, user, remote_path)

        if not success:
            log_error(f"Deploy to {node_name} failed")
            return False

        # Post-deploy cleanup of old residual files
        log_step(f"Cleanup old residuals ({node_name})")
        cleanup_files = [
            "tical_code/core/capabilities.py",  # v0.5+ removed, prompt_generator.py is the only source
            "tical_code/core/capabilities.pyc",
            "tical_code/core/__pycache__/capabilities*.pyc",
        ]
        for old_file in cleanup_files:
            rm_cmd = f"rm -f {remote_path}/{old_file}"
            self._ssh_exec(host, user, rm_cmd, check=False)

        # Push standard worker entry script
        log_step(f"Push standard entry script ({node_name})")
        self._deploy_worker_entry(host, user, remote_path)

        # Post-deploy locking
        log_step(f"Lock core files ({node_name})")
        lock_cmd = f"cd {remote_path} && bash secure_runtime.sh lock 2>/dev/null || chmod 444 tical_code/core/memory_evolve.py tical_code/core/sandbox.py tical_code/core/tool_router.py 2>/dev/null"
        self._ssh_exec(host, user, lock_cmd, check=False)

        # Verify
        if verify:
            return self.verify_node(node_name)
        
        log_info(f"Deploy to {node_name} complete ✓")
        return True

    def _rsync_deploy(self, files: List[str], host: str, user: str, remote_path: str) -> bool:
        """rsync Deploy"""
        # Unlock first
        unlock_cmd = f"cd {remote_path} && bash secure_runtime.sh unlock 2>/dev/null || chmod 644 tical_code/core/*.py 2>/dev/null"
        self._ssh_exec(host, user, unlock_cmd, check=False)

        # Build rsync command
        file_list = " ".join(files)
        cmd = [
            "rsync", "-avz", "--relative",
            "--files-from=-", ".",
            f"{user}@{host}:{remote_path}"
        ]
        
        try:
            result = subprocess.run(
                cmd,
                input="\n".join(files),
                cwd=str(self.project_dir),
                capture_output=True,
                text=True,
                timeout=120,
            )
            if result.returncode != 0:
                log_error(f"rsync failed: {result.stderr}")
                return False
            return True
        except FileNotFoundError:
            log_warn("rsync unavailable, falling back to tar+ssh")
            return self._tar_ssh_deploy(files, host, user, remote_path)
        except subprocess.TimeoutExpired:
            log_error("rsync timeout")
            return False

    def _tar_ssh_deploy(self, files: List[str], host: str, user: str, remote_path: str) -> bool:
        """tar+ssh Deploy (fallback)"""
        # Unlock first
        unlock_cmd = f"cd {remote_path} && bash secure_runtime.sh unlock 2>/dev/null || chmod 644 tical_code/core/*.py 2>/dev/null"
        self._ssh_exec(host, user, unlock_cmd, check=False)

        # Create temporary tar archive
        with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
            tmp_path = tmp.name
        
        try:
            with tarfile.open(tmp_path, "w:gz") as tar:
                for f in files:
                    full_path = self.project_dir / f
                    if full_path.exists():
                        tar.add(str(full_path), arcname=f)
            
            # SCP upload
            log_info(f"Upload tar archive ({os.path.getsize(tmp_path)//1024}KB)")
            scp_cmd = ["scp", "-q", tmp_path, f"{user}@{host}:/tmp/tical-deploy.tar.gz"]
            result = subprocess.run(scp_cmd, capture_output=True, text=True, timeout=120)
            if result.returncode != 0:
                log_error(f"SCP failed: {result.stderr}")
                return False

            # Remote extraction
            extract_cmd = f"cd {remote_path} && tar xzf /tmp/tical-deploy.tar.gz && rm /tmp/tical-deploy.tar.gz"
            return self._ssh_exec(host, user, extract_cmd)
        
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

    def _deploy_worker_entry(self, host: str, user: str, remote_path: str):
        """Deploy standard worker entry script to remote.

        v0.5.3+: Push scripts/run_worker.py, use PromptGenerator (only source).
        If remote has old worker.py that uses capabilities.generate_prompt, give migration hint.
        """
        # Push standard entry
        local_entry = self.project_dir / "scripts" / "run_worker.py"
        if local_entry.exists():
            scp_cmd = ["scp", "-o", "ConnectTimeout=10", "-o", "StrictHostKeyChecking=no",
                       str(local_entry), f"{user}@{host}:{remote_path}/run_worker.py"]
            try:
                result = subprocess.run(scp_cmd, capture_output=True, text=True, timeout=30)
                if result.returncode == 0:
                    log_info(f"Standard entry pushed: run_worker.py")
                else:
                    log_warn(f"Push entry failed: {result.stderr}")
            except Exception as e:
                log_warn(f"Push entry exception: {e}")
        else:
            log_warn("Local scripts/run_worker.py not found, skipping")

        # Check if remote old worker.py still uses capabilities
        check_cmd = (
            f'grep -l "capabilities" {remote_path}/worker.py 2>/dev/null && '
            f'echo "OLD_FORMAT" || echo "OK"'
        )
        ssh_cmd = ["ssh", "-o", "ConnectTimeout=10", "-o", "StrictHostKeyChecking=no",
                    f"{user}@{host}", check_cmd]
        try:
            result = subprocess.run(ssh_cmd, capture_output=True, text=True, timeout=15)
            if "OLD_FORMAT" in result.stdout:
                log_warn(
                    f"Remote worker.py still uses old capabilities API!\n"
                    f"  Migration: change worker.py to call run_worker.py,\n"
                    f"  or manually replace import+HUMAN_PROMPT with PromptGenerator (see scripts/run_worker.py)"
                )
        except Exception:
            pass  # Detection failure does not affect deployment

    def _ssh_exec(self, host: str, user: str, command: str, check: bool = True) -> bool:
        """Execute remote SSH command"""
        cmd = ["ssh", "-o", "ConnectTimeout=10", "-o", "StrictHostKeyChecking=no",
               f"{user}@{host}", command]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            if check and result.returncode != 0:
                log_error(f"SSH command failed: {result.stderr}")
                return False
            return True
        except subprocess.TimeoutExpired:
            log_error("SSH timeout")
            return False
        except FileNotFoundError:
            log_error("ssh command unavailable")
            return False

    def verify_node(self, node_name: str) -> bool:
        """Verify remote version"""
        node = self.nodes.get(node_name)
        if not node:
            return False

        host = node["host"]
        user = node["user"]
        remote_path = node["path"]

        log_step(f"Verify {node_name} version...")
        verify_cmd = f'cd {remote_path} && python3 -c "from tical_code import __version__; print(__version__)"'
        
        cmd = ["ssh", "-o", "ConnectTimeout=10", "-o", "StrictHostKeyChecking=no",
               f"{user}@{host}", verify_cmd]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if result.returncode != 0:
                log_error(f"Verification failed: {result.stderr}")
                return False
            
            remote_version = result.stdout.strip()
            local_version = self.get_version()
            
            if remote_version == local_version:
                log_info(f"{node_name}: Version match {remote_version} ✓")
                return True
            else:
                log_error(f"{node_name}: Version mismatch! Local={local_version}, Remote={remote_version}")
                return False
        except Exception as e:
            log_error(f"Verification exception: {e}")
            return False


# =============================================================================
# CLI
# =============================================================================

def main():
    parser = argparse.ArgumentParser(description="tical-code multi-node deployment tool")
    parser.add_argument("--target", required=True,
                       help="Deployment target: node-a, node-b, node-c, all")
    parser.add_argument("--verify", action="store_true",
                       help="Verify version after deployment")
    parser.add_argument("--config", default=None,
                       help="Custom deploy config file path")
    parser.add_argument("--dry-run", action="store_true",
                       help="Show files to sync only, no actual deployment")
    
    args = parser.parse_args()

    project_dir = os.path.dirname(os.path.abspath(__file__))
    deployer = Deployer(project_dir, args.config)

    if args.dry_run:
        files = deployer.collect_files()
        version = deployer.get_version()
        print(f"tical-code {version} - {len(files)} files pending sync:")
        for f in files:
            print(f"  {f}")
        return

    targets = []
    if args.target == "all":
        targets = [n for n in deployer.nodes if deployer.nodes[n]["host"]]
    else:
        targets = [args.target]

    if not targets:
        log_error("No deployable nodes (host not configured)")
        sys.exit(1)

    version = deployer.get_version()
    log_info(f"tical-code {version} → {targets}")

    results = {}
    for target in targets:
        results[target] = deployer.deploy_to_node(target, verify=args.verify)

    # Summary
    print()
    log_info("Deployment results summary:")
    all_ok = True
    for target, ok in results.items():
        status = f"{GREEN}✓{NC}" if ok else f"{RED}✗{NC}"
        print(f"  {target}: {status}")
        if not ok:
            all_ok = False
    
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()

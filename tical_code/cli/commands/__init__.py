# eite-agent -- Existential Identity Test Engine
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
CLI Commands
============

Modular CLI commands for eite-agent.
"""

import sys
import click
from typing import Optional, List


# =============================================================================
# Base Command Group
# =============================================================================

@click.group()
@click.version_option(version="0.1.3", prog_name="tical")
def cli():
    """
    eite-agent: Existential Identity Test Engine
    
    Core philosophy: "Do NOT trust AI output, assume AI will hallucinate"
    """
    pass


# =============================================================================
# Setup Command
# =============================================================================

@cli.command()
@click.option('--edition', type=click.Choice(['auto', 'lite', 'full']), default='auto',
              help='Edition to setup (auto-detect by default)')
@click.option('--force', is_flag=True, help='Force re-setup even if already configured')
def setup(edition: str, force: bool):
    """
    Setup eite-agent configuration.
    
    Auto-detects system capabilities and configures the appropriate edition.
    """
    from tical_code.core.detection import detect_edition, print_detection_report, SystemProfile
    
    # Print detection report
    profile = print_detection_report()
    
    # Determine edition
    if edition == 'auto':
        edition = profile.recommended_edition()
    
    click.echo(f"\nSetting up eite-agent {edition.upper()} edition...")
    
    # Initialize config
    from tical_code.cli.config import get_config
    
    config = get_config()
    current_edition = config.get('edition')
    
    if current_edition and current_edition == edition and not force:
        click.echo(f"Already configured for {edition} edition. Use --force to re-setup.")
        return
    
    # Update config
    config.set('edition', edition)
    
    # Create necessary directories
    import os
    for dir_key in ['log_dir', 'data_dir', 'plugins_dir']:
        dir_path = config.get(dir_key)
        if dir_path:
            os.makedirs(dir_path, exist_ok=True)
    
    click.echo(f"[OK] Configured for {edition.upper()} edition")
    click.echo(f"  Config file: {config.config_file}")


# =============================================================================
# Config Commands
# =============================================================================

@cli.group('config')
def config_group():
    """Manage eite-agent configuration."""
    pass


@config_group.command('get')
@click.argument('key', required=False)
def config_get(key: Optional[str]):
    """Get configuration value(s)."""
    from tical_code.cli.config import print_config
    print_config(key)


@config_group.command('set')
@click.argument('key')
@click.argument('value')
def config_set(key: str, value: str):
    """Set a configuration value."""
    from tical_code.cli.config import set_config
    set_config(key, value)


@config_group.command('list')
def config_list():
    """List all configuration keys with their default values."""
    from tical_code.cli.config import DEFAULT_CONFIG
    import json
    print(json.dumps(DEFAULT_CONFIG, indent=2))


@config_group.command('reset')
@click.confirmation_option(prompt='Reset all configuration to defaults?')
def config_reset():
    """Reset configuration to defaults."""
    from tical_code.cli.config import get_config
    config = get_config()
    config.reset()
    click.echo("Configuration reset to defaults.")


# =============================================================================
# Worker Commands
# =============================================================================

@cli.group('worker')
def worker_group():
    """Manage worker nodes."""
    pass


@worker_group.command('list')
@click.option('--status', help='Filter by status')
def worker_list(status: Optional[str]):
    """List all workers."""
    from tical_code.core.worker import get_worker_pool, WorkerStatus
    
    pool = get_worker_pool()
    workers = pool.list_workers()
    
    if not workers:
        click.echo("No workers configured.")
        return
    
    for worker in workers:
        status_str = worker.status.value
        if status and worker.status.value != status:
            continue
        
        click.echo(f"{worker.name}: {worker.host}:{worker.port} [{status_str}]")


@worker_group.command('add')
@click.argument('name')
@click.argument('host')
@click.option('--port', default=22, help='SSH port')
@click.option('--user', default='root', help='SSH user')
@click.option('--identity-file', help='SSH identity file')
def worker_add(name: str, host: str, port: int, user: str, identity_file: Optional[str]):
    """Add a new worker."""
    from tical_code.core.worker import get_worker_pool, WorkerInfo
    
    pool = get_worker_pool()
    
    worker = WorkerInfo(
        name=name,
        host=host,
        port=port,
        user=user,
        identity_file=identity_file,
    )
    
    pool.add_worker(worker)
    click.echo(f"Added worker: {name} ({host}:{port})")


@worker_group.command('remove')
@click.argument('name')
@click.confirmation_option(prompt='Remove this worker?')
def worker_remove(name: str):
    """Remove a worker."""
    from tical_code.core.worker import get_worker_pool
    
    pool = get_worker_pool()
    pool.remove_worker(name)
    click.echo(f"Removed worker: {name}")


@worker_group.command('ping')
@click.argument('name')
async def worker_ping(name: str):
    """Ping a worker to check connectivity."""
    import asyncio
    from tical_code.core.worker import get_worker_pool
    
    pool = get_worker_pool()
    
    result = await pool.execute_on(name, "echo 'pong'")
    
    if result.success:
        click.echo(f"[OK] {name}: OK ({result.elapsed_ms:.1f}ms)")
    else:
        click.echo(f"[FAIL] {name}: FAILED - {result.error}")


# =============================================================================
# Anchor Commands
# =============================================================================

@cli.group('anchor')
def anchor_group():
    """Manage bootstrap anchors."""
    pass


@anchor_group.command('list')
def anchor_list():
    """List all anchors."""
    from tical_code.core.anchor import get_anchor_manager
    
    manager = get_anchor_manager()
    anchors = manager.get_valid_anchors()
    
    if not anchors:
        click.echo("No anchors configured.")
        return
    
    for anchor in anchors:
        click.echo(f"[{anchor.anchor_type.value}] {anchor.key}")
        click.echo(f"  value: {anchor.value}")
        click.echo(f"  confidence: {anchor.confidence}")


@anchor_group.command('get')
@click.argument('anchor_type')
@click.argument('key')
def anchor_get(anchor_type: str, key: str):
    """Get a specific anchor."""
    from tical_code.core.anchor import get_anchor_manager, AnchorType
    
    manager = get_anchor_manager()
    
    try:
        at = AnchorType(anchor_type)
        anchor = manager.get(at, key)
        
        if anchor:
            import json
            click.echo(json.dumps(anchor.to_dict(), indent=2))
        else:
            click.echo(f"Anchor not found: {anchor_type}/{key}")
    except ValueError:
        click.echo(f"Invalid anchor type: {anchor_type}")


@anchor_group.command('context')
def anchor_context():
    """Generate anchor context prompt."""
    from tical_code.core.anchor import get_anchor_manager
    
    manager = get_anchor_manager()
    context = manager.get_context_prompt()
    
    click.echo(context)


# =============================================================================
# Memory Commands
# =============================================================================

@cli.group('memory')
def memory_group():
    """Manage memory store."""
    pass


@memory_group.command('stats')
def memory_stats():
    """Show memory store statistics."""
    from tical_code.core.memory import get_memory_store
    
    store = get_memory_store()
    stats = store.get_stats()
    
    import json
    click.echo(json.dumps(stats, indent=2))


@memory_group.command('list')
@click.option('--type', 'memory_type', help='Filter by memory type')
def memory_list(memory_type: Optional[str]):
    """List memory entries."""
    from tical_code.core.memory import get_memory_store, MemoryType
    
    store = get_memory_store()
    
    if memory_type:
        try:
            mt = MemoryType(memory_type)
            entries = store.get_by_type(mt)
        except ValueError:
            click.echo(f"Invalid memory type: {memory_type}")
            return
    else:
        entries = list(store.entries.values())
    
    if not entries:
        click.echo("No memory entries.")
        return
    
    for entry in entries:
        click.echo(f"[{entry.memory_type.value}] {entry.key}")
        click.echo(f"  accesses: {entry.access_count}, age: {entry.get_age():.0f}s")


# =============================================================================
# Verify Command
# =============================================================================

@cli.command()
@click.argument('command')
def verify(command: str):
    """
    Verify a command/output.
    
    Run Force-Verify on the given command or output.
    """
    from tical_code.core.verify import VerifyLevel, SchemaValidator
    
    click.echo(f"Verifying: {command}")
    
    # Basic verification
    result = SchemaValidator.validate(
        command,
        {"type": "string"}
    )
    
    if result.passed:
        click.echo("[OK] Verification passed")
    else:
        click.echo(f"[FAIL] Verification failed: {result.details}")


# =============================================================================
# Status Command
# =============================================================================

@cli.command()
def status():
    """Show eite-agent status."""
    from tical_code.core.detection import detect_edition, SystemProfile
    from tical_code.core.worker import get_worker_pool
    from tical_code.core.anchor import get_anchor_manager
    from tical_code.core.memory import get_memory_store
    from tical_code.core.errors import get_error_logger
    from tical_code.cli.config import get_config
    
    config = get_config()
    edition = config.get('edition', 'unknown')
    
    click.echo("=" * 50)
    click.echo("eite-agent v0.1.3 Status")
    click.echo("=" * 50)
    click.echo(f"Edition: {edition}")
    click.echo(f"Config: {config.config_file}")
    
    # Workers
    pool = get_worker_pool()
    workers = pool.list_workers()
    click.echo(f"\nWorkers: {len(workers)}")
    for w in workers[:3]:
        click.echo(f"  - {w.name}: {w.status.value}")
    
    # Anchors
    manager = get_anchor_manager()
    anchors = manager.get_valid_anchors()
    click.echo(f"\nAnchors: {len(anchors)}")
    
    # Memory
    store = get_memory_store()
    stats = store.get_stats()
    click.echo(f"\nMemory: {stats['total_entries']} entries")
    
    # Errors
    error_logger = get_error_logger()
    error_stats = error_logger.get_error_stats()
    click.echo(f"\nErrors: {error_stats['unresolved']} unresolved")
    
    click.echo("=" * 50)


# =============================================================================
# Detection Command
# =============================================================================

@cli.command()
def detect():
    """Detect system capabilities and recommend edition."""
    from tical_code.core.detection import print_detection_report
    print_detection_report()


# =============================================================================
# Run Command -- start a worker
# =============================================================================

@cli.command()
@click.option('--worker', default='default', help='Worker name')
@click.option('--config', 'cfg_path', default=None, help='Path to worker config JSON')
@click.option('--daemon', is_flag=True, help='Run as daemon (fork to background)')
def run(worker: str, cfg_path: Optional[str], daemon: bool):
    """Start an eite-agent worker (the main agent loop).

    Launches the unified worker with provider auto-discovery,
    permission system, and tool execution. Blocks until SIGINT.
    """
    import asyncio
    from tical_code.core.provider_registry import from_registry

    click.echo(f"Starting eite-agent worker: {worker}")
    click.echo("Discovering providers...")
    mf = from_registry(worker_name=worker)
    click.echo(f"  {len(mf.providers)} provider(s) active")

    if daemon:
        click.echo("Daemon mode not yet implemented -- running in foreground.")
    click.echo("Worker running. Press Ctrl+C to stop.")

    try:
        from tical_code.core.unified_worker import start_worker
        asyncio.run(start_worker(worker_name=worker, failover=mf))
    except KeyboardInterrupt:
        click.echo("\nWorker stopped.")
    except ImportError:
        click.echo("Starting minimal loop (unified_worker not available)...")
        # Fallback: just keep alive
        import time, signal
        stop = False
        def _sig(s, f):
            nonlocal stop
            stop = True
        signal.signal(signal.SIGINT, _sig)
        signal.signal(signal.SIGTERM, _sig)
        while not stop:
            time.sleep(1)
        click.echo("Stopped.")


# =============================================================================
# Init Command -- create default config files
# =============================================================================

@cli.command()
@click.option('--edition', type=click.Choice(['auto', 'lite', 'full']), default='auto',
              help='Edition to initialize')
@click.option('--force', is_flag=True, help='Overwrite existing config')
def init(edition: str, force: bool):
    """Create default configuration and directory structure.

    Sets up config/providers.json, worker-config templates,
    and the ~/.tical/ directory structure needed to run.
    """
    import json, os
    from pathlib import Path
    from tical_code.cli.config import get_config

    # Create config directory structure
    repo_root = Path(os.environ.get("TICAL_CODE_ROOT", os.getcwd()))
    config_dir = repo_root / "config"
    worker_configs_dir = config_dir / "worker-configs"
    config_dir.mkdir(parents=True, exist_ok=True)
    worker_configs_dir.mkdir(parents=True, exist_ok=True)

    # providers.json (default with auto-discover comment)
    providers_file = config_dir / "providers.json"
    if not providers_file.exists() or force:
        default_providers = {
            "_comment": "Provider registry -- add any OpenAI-compatible provider here.",
            "_auto_discover": "Set DEEPSEEK_API_KEY, OPENAI_API_KEY, etc. in env to auto-discover. This file is optional.",
            "providers": {}
        }
        with open(providers_file, "w") as f:
            json.dump(default_providers, f, indent=2)
        click.echo(f"  [OK] {providers_file}")

    # default.json
    default_file = config_dir / "default.json"
    if not default_file.exists() or force:
        with open(default_file, "w") as f:
            json.dump({
                "name": "default",
                "edition": edition,
                "log_level": "INFO",
                "providers": ["deepseek", "openai", "openrouter"]
            }, f, indent=2)
        click.echo(f"  [OK] {default_file}")

    # ~/.tical/ directory
    home_config = Path.home() / ".tical"
    home_config.mkdir(parents=True, exist_ok=True)
    click.echo(f"  [OK] {home_config}/")

    click.echo("Initialization complete.")
    click.echo("Next: set your API key and run 'tical run'")


# =============================================================================
# Backup / Rollback Commands -- snapshot & restore state
# =============================================================================

@cli.command()
@click.option('--name', default=None, help='Snapshot name (default: auto timestamp)')
@click.option('--path', default=None, help='Custom snapshot directory')
def backup(name, path):
    """Create a snapshot of current agent state (config, memory, anchors).

    Saves config/providers.json, config/default.json, worker-configs/*,
    and ~/.tical/* to a timestamped directory under .tical-snapshots/.
    """
    import json, os, shutil, datetime
    from pathlib import Path

    repo = Path(os.environ.get("TICAL_CODE_ROOT", os.getcwd()))
    snap_root = repo / ".tical-snapshots"
    snap_root.mkdir(parents=True, exist_ok=True)

    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    snap_name = name or f"snapshot_{ts}"
    snap_dir = snap_root / snap_name
    snap_dir.mkdir(parents=True, exist_ok=True)

    # Config files
    config_dir = repo / "config"
    if config_dir.exists():
        shutil.copytree(config_dir, snap_dir / "config", dirs_exist_ok=True)

    # User data dir
    home_cfg = Path.home() / ".tical"
    if home_cfg.exists():
        shutil.copytree(home_cfg, snap_dir / "dot_tical", dirs_exist_ok=True)

    # Save metadata
    with open(snap_dir / "meta.json", "w") as f:
        json.dump({
            "name": snap_name,
            "created": ts,
            "tical_code_root": str(repo),
        }, f, indent=2)

    click.echo(f"[OK] Backup saved: {snap_dir}")


@cli.command()
@click.argument('snapshot', required=False)
@click.option('--list', 'list_only', is_flag=True, help='List available snapshots')
def rollback(snapshot, list_only):
    """Restore agent state from a previous backup snapshot.

    Lists snapshots if run without arguments. Apply with --list first
    to find the snapshot name, then: tical rollback <name>
    """
    import shutil, os
    from pathlib import Path

    repo = Path(os.environ.get("TICAL_CODE_ROOT", os.getcwd()))
    snap_root = repo / ".tical-snapshots"

    if not snap_root.exists():
        click.echo("No snapshots found (run 'tical backup' first)")
        return

    snaps = sorted([d.name for d in snap_root.iterdir() if d.is_dir()])

    if list_only or not snapshot:
        click.echo("Available snapshots:")
        for s in snaps:
            size = sum(f.stat().st_size for f in (snap_root / s).rglob("*") if f.is_file())
            click.echo(f"  {s}  ({size // 1024} KB)")
        return

    snap_dir = snap_root / snapshot
    if not snap_dir.exists():
        click.echo(f"Snapshot not found: {snapshot}")
        return

    # Restore config
    src_config = snap_dir / "config"
    dst_config = repo / "config"
    if src_config.exists():
        if dst_config.exists():
            shutil.rmtree(dst_config)
        shutil.copytree(src_config, dst_config)
        click.echo(f"  [OK] config/ restored")

    # Restore ~/.tical
    src_dot = snap_dir / "dot_tical"
    dst_dot = Path.home() / ".tical"
    if src_dot.exists():
        if dst_dot.exists():
            shutil.rmtree(dst_dot)
        shutil.copytree(src_dot, dst_dot)
        click.echo(f"  [OK] ~/.tical/ restored")

    click.echo(f"[OK] Rolled back to: {snapshot}")


# =============================================================================
# Export CLI
# =============================================================================

def main():
    """Main entry point."""
    cli()


if __name__ == '__main__':
    main()


# =============================================================================
# v0.3 New Commands - Import and Register
# =============================================================================

# Import new command modules
try:
    from . import workflow
    from . import eval
except ImportError:
    pass
else:
    # Register workflow commands
    cli.add_command(workflow.workflow_group)
    
    # Register eval commands
    cli.add_command(eval.eval_group)
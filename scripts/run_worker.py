#!/usr/bin/env python3

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

"""
EITElite Worker Entry Script
===========================

This is the standard way to start a EITElite worker.

Usage:
    python scripts/run_worker.py --config config/worker-configs/tico-sg.json
    python scripts/run_worker.py --deploy-id tico-sg-v3
    python scripts/run_worker.py --config config/worker-configs/tico-sg.json --verify

v0.5.3+: Uses PromptGenerator as the sole source of identity declarations (no longer uses capabilities.py)
v0.6.0: Self-healing system - Fixes P0 bug (must enter main loop after bootstrap), signal recording, snapshot recovery
v0.6.1: P0 fatal bug fix - Explicitly call run_loop() after bootstrap(), no longer rely on create_task+run_forever

P0 Bug root cause:
    Old code: bootstrap() called create_task(run_loop) then returned immediately,
    run_worker.py used run_forever() to keep the event loop running,
    but signal handling did not call loop.stop(), so run_forever never exited,
    or run_forever kept spinning after run_loop exited abnormally.
    Fix: bootstrap() only initializes, run_loop() is explicitly awaited by this script,
    signals only set the _shutdown flag to let run_loop exit naturally, then cleanup runs after exit.

Author: EITElite Team
Version: see tical_code.__version__
"""

import argparse
import asyncio
import logging
import os
import signal
import sys
import time
import traceback
from pathlib import Path

# Ensure project root is in sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# death log directory
DEATH_LOG_DIR = os.path.expanduser("~/.EITElite/death-log")


def _write_death_log(worker_name: str, reason: str, details: str = ""):
    """
    Write death log to ~/.EITElite/death-log/<worker_name>_<timestamp>.log

    This is the last resort: even if the session_snapshot module is unavailable,
    it leaves a death record on disk to prevent silent exits from being undiagnosable.
    """
    try:
        os.makedirs(DEATH_LOG_DIR, exist_ok=True)
        ts = time.strftime('%Y%m%d_%H%M%S')
        log_path = os.path.join(DEATH_LOG_DIR, f"{worker_name}_{ts}.log")
        with open(log_path, 'w', encoding='utf-8') as f:
            f.write(f"Worker: {worker_name}\n")
            f.write(f"Time: {ts}\n")
            f.write(f"Reason: {reason}\n")
            if details:
                f.write(f"Details:\n{details}\n")
    except Exception:
        # death log write failure must not raise another exception, or it would mask the original error
        pass


def main():
    parser = argparse.ArgumentParser(
        description='EITElite Worker - Autonomous AI Agent'
    )
    parser.add_argument(
        '--config', '-c',
        help='Worker config JSON file path',
        type=str,
    )
    parser.add_argument(
        '--deploy-id', '-d',
        help='Deploy ID from anchor.json',
        type=str,
    )
    parser.add_argument(
        '--anchor',
        default='anchor.json',
        help='Anchor file path (default: anchor.json)',
    )
    parser.add_argument(
        '--verify',
        action='store_true',
        help='Run verification after bootstrap',
    )
    parser.add_argument(
        '--log-level',
        default='INFO',
        choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'],
        help='Log level',
    )

    args = parser.parse_args()

    # Configure logging
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    )
    logger = logging.getLogger('run_worker')

    # Load config
    from tical_code.core.worker_framework import WorkerFramework, WorkerConfig

    if args.config:
        config = WorkerConfig.from_file(args.config)
    elif args.deploy_id:
        config = WorkerConfig.from_anchor(args.anchor, args.deploy_id)
    else:
        parser.error("Must specify --config or --deploy-id")
        return

    logger.info(f"EITElite Worker starting: name={config.name}, edition={config.edition}, model={config.model}")

    # Create Worker
    worker = WorkerFramework(config)

    # ================================================================
    # Event Loop + signal handling
    # ================================================================
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    # Record received signal for death log
    _received_signal = {'signo': None}

    def _signal_handler(signo):
        """
        Signal handler: set the shutdown flag to let run_loop exit naturally.

        No longer executes shutdown logic here - after run_loop exits,
        the main flow will sequentially run snapshot → death log → shutdown → close loop.
        """
        _received_signal['signo'] = signo
        sig_name = signal.Signals(signo).name if isinstance(signo, int) else str(signo)
        logger.info(f"Received signal {sig_name}({signo}), setting shutdown flag...")
        # Only set flag, let run_loop's while loop exit
        worker._shutdown.set()

    # Register SIGTERM / SIGINT signal handlers
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, lambda s=sig: _signal_handler(s))
        except NotImplementedError:
            # Windows doesn't support add_signal_handler, use signal.signal instead
            signal.signal(sig, lambda s, f, _sig=sig: _signal_handler(_sig))

    # ================================================================
    # Uncaught exception → death log
    # ================================================================
    def _handle_uncaught_exception(exc_type, exc_value, exc_tb):
        """Handle uncaught exceptions: record to death log (disk + session_snapshot)"""
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return

        logger.critical(f"Uncaught exception: {exc_value}")

        # Disk-level death log (fallback guarantee)
        _write_death_log(
            worker.config.name,
            reason="uncaught_exception",
            details=''.join(traceback.format_exception(exc_type, exc_value, exc_tb)),
        )

        # session_snapshot death log (structured record)
        try:
            from tical_code.core.session_snapshot import record_death
            record_death(
                worker.config.name,
                signal_type=0,  # 0 = abnormal exit
                uptime=time.time() - worker.start_time,
                loop_count=worker.loop_count,
                last_error=str(exc_value),
                session_status='uncaught_exception',
                traceback_str=''.join(traceback.format_exception(exc_type, exc_value, exc_tb)),
            )
        except Exception:
            pass  # death record failure must not re-raise

        # Call default handler
        sys.__excepthook__(exc_type, exc_value, exc_tb)

    sys.excepthook = _handle_uncaught_exception

    # ================================================================
    # Core flow: bootstrap → run_loop → cleanup → shutdown → close
    # ================================================================
    # P0 fix: bootstrap() only initializes, run_loop() is explicitly driven from here.
    # Old code problems:
    #   bootstrap() internally called create_task(run_loop) then returned,
    #   run_worker.py used run_forever() without proper exit conditions,
    #   signal handling didn't call loop.stop(), causing the worker to silently spin or exit directly.
    # Fix:
    #   1. bootstrap() only does initialization (no more create_task)
    #   2. loop.run_until_complete(worker.run_loop()) explicitly runs the main loop
    #   3. Signals only set _shutdown flag, run_loop exits naturally
    #   4. After exit, sequentially execute snapshot / death log / shutdown / close loop
    exit_code = 0
    try:
        # Phase 1: Bootstrap - initialize identity, tools, sessions, self-checks, etc.
        loop.run_until_complete(worker.bootstrap())
        logger.info("Worker bootstrap complete, ready to enter main loop")

        # Phase 2: Run main loop - until _shutdown is set
        # run_loop() internally runs while not self._shutdown.is_set(),
        # when SIGTERM/SIGINT is received, _signal_handler sets _shutdown,
        # run_loop exits naturally, control returns here.
        loop.run_until_complete(worker.run_loop())
        logger.info("Worker main loop exited")

    except KeyboardInterrupt:
        logger.info("User interrupt (KeyboardInterrupt)")
        _received_signal['signo'] = signal.SIGINT

    except SystemExit as e:
        exit_code = e.code if isinstance(e.code, int) else 1
        logger.info(f"SystemExit(code={exit_code})")

    except Exception as e:
        exit_code = 1
        logger.error(f"Worker run failed: {e}")
        traceback.print_exc()

        # Record fatal error to death log
        _write_death_log(
            worker.config.name,
            reason="fatal_error",
            details=traceback.format_exc(),
        )
        try:
            from tical_code.core.session_snapshot import record_death
            record_death(
                worker.config.name,
                signal_type=0,
                uptime=time.time() - worker.start_time,
                loop_count=worker.loop_count,
                last_error=str(e),
                session_status='fatal_error',
                traceback_str=traceback.format_exc(),
            )
        except Exception:
            pass

    finally:
        # Phase 3: Graceful shutdown - execute cleanup whether normal exit or exception

        # 3a. Save session snapshot (needed even for signal-triggered exits)
        try:
            from tical_code.core.session_snapshot import save_snapshot
            snapshot_data = worker._build_snapshot_data()
            if _received_signal['signo'] is not None:
                signo = _received_signal['signo']
                snapshot_data['exit_signal'] = signo
                snapshot_data['exit_reason'] = (
                    f"signal_{signal.Signals(signo).name}"
                    if isinstance(signo, int) else str(signo)
                )
            save_snapshot(worker.config.name, snapshot_data)
            logger.info("Shutdown snapshot saved")
        except ImportError:
            logger.debug("session_snapshot module unavailable, skipping snapshot save")
        except Exception as e:
            logger.warning(f"Snapshot save failed: {e}")

        # 3b. Record death log (record on signal-triggered exit)
        if _received_signal['signo'] is not None:
            signo = _received_signal['signo']
            sig_name = signal.Signals(signo).name if isinstance(signo, int) else str(signo)
            _write_death_log(
                worker.config.name,
                reason=f"signal_{sig_name}",
            )
            try:
                from tical_code.core.session_snapshot import record_death
                record_death(
                    worker.config.name,
                    signal_type=signo,
                    uptime=time.time() - worker.start_time,
                    loop_count=worker.loop_count,
                    last_error=worker.last_error,
                    session_status=(
                        worker.status.value
                        if hasattr(worker.status, 'value')
                        else str(worker.status)
                    ),
                )
                logger.info(f"Death record written (signal: {sig_name})")
            except ImportError:
                pass
            except Exception as e:
                logger.warning(f"Death record write failed: {e}")

        # 3c. Execute worker.shutdown() - save sessions, clean up resources
        # skip_death_record=True: because steps 3a/3b already wrote snapshot and death log,
        # writing again here would overwrite the real signal info
        try:
            loop.run_until_complete(worker.shutdown(skip_death_record=True))
            logger.info("Worker shutdown complete")
        except Exception as e:
            logger.warning(f"Shutdown failed: {e}")

        # 3d. Cancel all leftover tasks and close event loop
        try:
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True)
                )
        except Exception:
            pass
        finally:
            loop.close()
            logger.info("Event loop closed")

    sys.exit(exit_code)


if __name__ == '__main__':
    main()

#!/usr/bin/env python3
"""
Autonomous Loop Test Script
===============
Tests whether tical-code's AI worker can work autonomously and continuously.

Start worker, submit tasks, observe AI behavior:
- How many steps executed?
- Where did it stop?
- Did it proactively continue?
"""

import asyncio
import logging
import os
import sys
import time
import json
from pathlib import Path

# Ensure project root is in sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Set environment variables
os.environ['MIMO_API_KEY'] = '${MIMO_API_KEY}'

# Verbose logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger('autonomy_test')


async def main():
    """Main test flow"""
    from tical_code.core.worker_framework import WorkerFramework, WorkerConfig
    
    # Load config
    config_path = str(PROJECT_ROOT / 'config' / 'worker-configs' / 'kael-local.json')
    logger.info(f"Loading config: {config_path}")
    config = WorkerConfig.from_file(config_path)
    logger.info(f"Worker: name={config.name}, model={config.model}")
    
    # Create worker
    worker = WorkerFramework(config)
    
    # Bootstrap
    logger.info("=" * 60)
    logger.info("Phase 1: Bootstrap")
    logger.info("=" * 60)
    try:
        await worker.bootstrap()
        logger.info("Bootstrap complete")
    except Exception as e:
        logger.error(f"Bootstrap failed: {e}")
        import traceback
        traceback.print_exc()
        return
    
    # Read task file
    task_path = '/tmp/autonomy_task.md'
    with open(task_path, 'r', encoding='utf-8') as f:
        task_content = f.read()
    
    logger.info("=" * 60)
    logger.info("Phase 2: Submit task to AI worker")
    logger.info("=" * 60)
    logger.info(f"Task content length: {len(task_content)} chars")
    logger.info(f"Task first 100 chars: {task_content[:100]}")

    # Method 1: Use framework's handle_message
    # This is the standard message handling entry point
    user_id = "autonomy_tester"
    
    # Record start time
    start_time = time.time()

    # Submit task
    try:
        response = await worker.handle_message(user_id, task_content)
        elapsed = time.time() - start_time
        logger.info("=" * 60)
        logger.info(f"Phase 2 result (elapsed {elapsed:.1f}s):")
        logger.info("=" * 60)
        logger.info(f"AI response:\n{response}")
    except Exception as e:
        elapsed = time.time() - start_time
        logger.error(f"handle_message error (elapsed {elapsed:.1f}s): {e}")
        import traceback
        traceback.print_exc()
    
    # Try to continue - send "continue" command
    logger.info("=" * 60)
    logger.info('Phase 3: Manually send "continue" command')
    logger.info("=" * 60)
    
    continue_prompt = "Continue executing the task. Where did you leave off? Please continue the unfinished work."
    
    start_time2 = time.time()
    try:
        response2 = await worker.handle_message(user_id, continue_prompt)
        elapsed2 = time.time() - start_time2
        logger.info("=" * 60)
        logger.info(f"Phase 3 result (elapsed {elapsed2:.1f}s):")
        logger.info("=" * 60)
        logger.info(f"AI response:\n{response2}")
    except Exception as e:
        elapsed2 = time.time() - start_time2
        logger.error(f"handle_message error (elapsed {elapsed2:.1f}s): {e}")
        import traceback
        traceback.print_exc()
    
    # Try continue once more
    logger.info("=" * 60)
    logger.info('Phase 4: Second "continue" command')
    logger.info("=" * 60)
    
    continue_prompt2 = "Continue. Don't just analyze, actually execute: modify code, write files, run tests."
    
    start_time3 = time.time()
    try:
        response3 = await worker.handle_message(user_id, continue_prompt2)
        elapsed3 = time.time() - start_time3
        logger.info("=" * 60)
        logger.info(f"Phase 4 result (elapsed {elapsed3:.1f}s):")
        logger.info("=" * 60)
        logger.info(f"AI response:\n{response3}")
    except Exception as e:
        elapsed3 = time.time() - start_time3
        logger.error(f"handle_message error (elapsed {elapsed3:.1f}s): {e}")
        import traceback
        traceback.print_exc()
    
    # Check session history
    logger.info("=" * 60)
    logger.info("Phase 5: Session history check")
    logger.info("=" * 60)
    
    if worker.worker_loop:
        history = worker.worker_loop.get_session_history(user_id)
        logger.info(f"Session history message count: {len(history)}")
        for i, msg in enumerate(history):
            role = msg.get('role', '?')
            content_preview = msg.get('content', '')[:200]
            logger.info(f"  [{i}] {role}: {content_preview}...")
    
    # Shutdown
    logger.info("=" * 60)
    logger.info("Phase 6: Shutdown")
    logger.info("=" * 60)
    await worker.shutdown(skip_death_record=True)
    logger.info("Shutdown complete")


if __name__ == '__main__':
    asyncio.run(main())

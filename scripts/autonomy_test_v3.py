#!/usr/bin/env python3
"""
Autonomy loop test v3 - Fixed known bugs, truly tests AI autonomous capability
"""
import asyncio
import logging
import os
import sys
import time
import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

os.environ['MIMO_API_KEY'] = '${MIMO_API_KEY}'

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
)
logger = logging.getLogger('autonomy_v3')


async def test_workerloop_fixed():
    """Test: WorkerLoop + fixed model bug + disabled Clarify"""
    from tical_code.core.worker_framework import WorkerFramework, WorkerConfig
    from tical_code.core.worker_loop import WorkerLoop, WorkerLoopConfig, UserMessage

    config = WorkerConfig.from_file(str(PROJECT_ROOT / 'config' / 'worker-configs' / 'kael-local.json'))
    worker = WorkerFramework(config)
    await worker.bootstrap()

    # Fix 1: Disable Clarify (overly cautious engine blocks tasks)
    worker._has_clarify = False

    # Fix 2: Fix WorkerLoop LLM model setting
    if worker.worker_loop:
        worker.worker_loop.config.llm_model = 'mimo-v2-pro'
        # Re-init LLM
        worker.worker_loop._init_llm()
        logger.info(f"After fix LLM default model: {worker.worker_loop.config.llm_model}")
        logger.info(f"LLM available: {worker.worker_loop._llm.is_available()}")
        logger.info(f"LLM available models: {worker.worker_loop._llm.get_available_models()}")

    task = open('/tmp/autonomy_task.md', 'r').read()

    logger.info("=" * 60)
    logger.info("Submitting task to AI worker (fixed)")
    logger.info("=" * 60)

    user_id = 'autonomy_v3'

    # Phase 1: Submit task
    start1 = time.time()
    try:
        response = await worker.handle_message(user_id, task)
        elapsed1 = time.time() - start1
        logger.info(f"Phase 1 done (took {elapsed1:.1f}s):")
        logger.info(f"AI reply ({len(response)} chars):\n{response[:3000]}")
    except Exception as e:
        elapsed1 = time.time() - start1
        logger.error(f"Phase 1 error (took {elapsed1:.1f}s): {e}")
        import traceback
        traceback.print_exc()
        response = "ERROR"

    # Phase 2: Continue
    logger.info("=" * 60)
    logger.info("Phase 2: Continue")
    logger.info("=" * 60)

    start2 = time.time()
    try:
        response2 = await worker.handle_message(user_id, "Continue executing the task. Don't just read code - actually modify code to implement the autonomy loop.")
        elapsed2 = time.time() - start2
        logger.info(f"Phase 2 done (took {elapsed2:.1f}s):")
        logger.info(f"AI reply ({len(response2)} chars):\n{response2[:3000]}")
    except Exception as e:
        elapsed2 = time.time() - start2
        logger.error(f"Phase 2 error (took {elapsed2:.1f}s): {e}")
        import traceback
        traceback.print_exc()
        response2 = "ERROR"

    # Phase 3: Continue
    logger.info("=" * 60)
    logger.info("Phase 3: Continue")
    logger.info("=" * 60)

    start3 = time.time()
    try:
        response3 = await worker.handle_message(user_id, "Continue. Now please write code. Use the write_file tool to write the modified worker_loop.py.")
        elapsed3 = time.time() - start3
        logger.info(f"Phase 3 done (took {elapsed3:.1f}s):")
        logger.info(f"AI reply ({len(response3)} chars):\n{response3[:3000]}")
    except Exception as e:
        elapsed3 = time.time() - start3
        logger.error(f"Phase 3 error (took {elapsed3:.1f}s): {e}")
        import traceback
        traceback.print_exc()
        response3 = "ERROR"

    # Check session history
    logger.info("=" * 60)
    logger.info("Session history")
    logger.info("=" * 60)

    if worker.worker_loop:
        history = worker.worker_loop.get_session_history(user_id)
        logger.info(f"Total messages: {len(history)}")
        for i, msg in enumerate(history):
            role = msg.get('role', '?')
            content = msg.get('content', '')
            # Count tool calls
            from tical_code.core.tool_call_parser import parse_tool_calls
            tools = parse_tool_calls(content)
            tool_names = [tc.get('tool') for tc in tools]
            logger.info(f"  [{i}] {role} ({len(content)} chars, tools={tool_names}): {content[:200]}")


    await worker.shutdown(skip_death_record=True)


async def test_manual_autonomy_loop():
    """Test: Manual autonomy loop (bypasses all WorkerLoop middle layers)"""
    from tical_code.core.llm_interface import LLMInterface
    from tical_code.core.tool_call_parser import parse_tool_calls, format_tool_result
    from tical_code.core.worker_framework import WorkerFramework, WorkerConfig

    config = WorkerConfig.from_file(str(PROJECT_ROOT / 'config' / 'worker-configs' / 'kael-local.json'))
    worker = WorkerFramework(config)
    await worker.bootstrap()

    llm = LLMInterface({'default_model': 'mimo-v2-pro'})

    task = open('/tmp/autonomy_task.md', 'r').read()

    system_prompt = f"""You are kael, an autonomous AI worker running on tical-code v0.5.8.

Your core ability is autonomous programming - no human guidance needed; you read code, design, write code, and test on your own.

Available tools:
1. read_file(path) - Read file
2. write_file(path, content) - Write file
3. shell_exec(cmd) - Execute shell command
4. search_files(pattern, directory) - Search files
5. patch_file(path, old_content, new_content) - Modify file content partially

Tool call format:
```json
{{"tool": "tool_name", "params": {{"param_name": "param_value"}}}}
```

Key requirements:
- You must keep working until the task is done
- After reading code, write code - don't just read without writing
- Call a tool at every step - no empty talk
- Output TASK_COMPLETE when done
- On failure, retry or switch approach"""

    messages = [{'role': 'user', 'content': task}]

    logger.info("=" * 60)
    logger.info("Manual autonomy loop test")
    logger.info("=" * 60)

    steps = []
    max_iterations = 15
    consecutive_no_tools = 0

    for iteration in range(1, max_iterations + 1):
        logger.info(f"\n{'='*40}")
        logger.info(f"Iteration {iteration}")
        logger.info(f"{'='*40}")

        start = time.time()
        try:
            result = await llm.chat(messages=messages, system_prompt=system_prompt, stream=False)
            elapsed = time.time() - start
        except Exception as e:
            elapsed = time.time() - start
            logger.error(f"LLM call failed: {e}")
            steps.append({'iteration': iteration, 'error': str(e), 'elapsed': elapsed})
            # Timeout retry
            if 'timed out' in str(e):
                logger.info("Timeout, waiting 5s before retry...")
                await asyncio.sleep(5)
                continue
            break

        content = result.get('content', '')
        logger.info(f"AI reply (took {elapsed:.1f}s, {len(content)} chars):")
        # Only print first 2000 chars
        logger.info(content[:2000])
        if len(content) > 2000:
            logger.info(f"... (omitted {len(content)-2000} chars)")

        tool_calls = parse_tool_calls(content)
        tool_names = [tc.get('tool') for tc in tool_calls]
        logger.info(f"Tool calls: {len(tool_calls)} → {tool_names}")

        step = {
            'iteration': iteration,
            'content_length': len(content),
            'tool_calls': len(tool_calls),
            'tool_names': tool_names,
            'elapsed': elapsed,
            'content_preview': content[:500],
        }
        steps.append(step)

        # Check task completion
        if not tool_calls:
            consecutive_no_tools += 1
            if 'TASK_COMPLETE' in content:
                logger.info("✅ Task complete!")
                break
            if consecutive_no_tools >= 2:
                logger.info("⚠️ 2 consecutive rounds with no tool calls, AI stopped")
                break
        else:
            consecutive_no_tools = 0

        # Execute tools
        tool_results = []
        for tc in tool_calls:
            tool_name = tc.get('tool', 'unknown')
            params = tc.get('params', {})
            logger.info(f"  Execute: {tool_name}({json.dumps(params, ensure_ascii=False)[:100]})")

            try:
                result_obj = await worker.worker_loop._execute_single_tool(tc)
                result_text = format_tool_result(result_obj.tool_name, result_obj.data, result_obj.error)
                # Truncate overly long results
                if len(result_text) > 3000:
                    result_text = result_text[:3000] + f"\n... (truncated, original length {len(result_text)})"
                logger.info(f"  Result: {result_text[:300]}")
                tool_results.append(result_text)
            except Exception as e:
                error_text = f"Tool execution error: {e}"
                logger.error(f"  {error_text}")
                tool_results.append(error_text)

        # Update messages
        messages.append({'role': 'assistant', 'content': content})
        for tr in tool_results:
            messages.append({'role': 'user', 'content': f"[Tool result]\n{tr}"})

        # Context management: if messages are too long, compress early messages
        total_chars = sum(len(m.get('content', '')) for m in messages)
        if total_chars > 30000:
            logger.info(f"Context too long ({total_chars} chars), compressing...")
            # Keep first user message (task) and recent messages
            if len(messages) > 8:
                task_msg = messages[0]
                recent = messages[-6:]
                # Add summary
                summary = f"[History summary] Completed {iteration} iterations, executed {sum(s.get('tool_calls',0) for s in steps)} tool calls."
                messages = [task_msg, {'role': 'system', 'content': summary}] + recent
                logger.info(f"Messages after compression: {len(messages)}")

    # Save results
    report_path = '/tmp/autonomy_v3_steps.json'
    with open(report_path, 'w', encoding='utf-8') as f:
        json.dump(steps, f, ensure_ascii=False, indent=2)
    logger.info(f"\nStep log: {report_path}")

    logger.info(f"\nTotal: {len(steps)} iterations")
    for step in steps:
        if 'error' in step:
            logger.info(f"  Iteration {step['iteration']}: ERROR - {step['error'][:80]}")
        else:
            logger.info(f"  Iteration {step['iteration']}: tools={step['tool_calls']} {step['tool_names']} ({step['elapsed']:.1f}s)")

    await worker.shutdown(skip_death_record=True)
    return steps


async def main():
    # Test A: WorkerLoop (after fix)
    logger.info("\n" + "#" * 80)
    logger.info("# Test A: WorkerLoop (fixed model + disabled Clarify)")
    logger.info("#" * 80)
    try:
        await test_workerloop_fixed()
    except Exception as e:
        logger.error(f"Test A failed: {e}")
        import traceback
        traceback.print_exc()

    # Test B: Manual autonomy loop
    logger.info("\n" + "#" * 80)
    logger.info("# Test B: Manual autonomy loop (15-iteration max)")
    logger.info("#" * 80)
    try:
        await test_manual_autonomy_loop()
    except Exception as e:
        logger.error(f"Test B failed: {e}")
        import traceback
        traceback.print_exc()


if __name__ == '__main__':
    asyncio.run(main())

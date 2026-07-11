#!/usr/bin/env python3
"""
Autonomy loop test v2 - bypass known issues, directly test AI worker core capabilities
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
logger = logging.getLogger('autonomy_v2')


async def test_direct_llm():
    """Test 1: Direct LLM call, see if AI can understand tasks and output tool calls"""
    from tical_code.core.llm_interface import LLMInterface
    
    llm = LLMInterface({'default_model': 'mimo-v2-pro'})
    
    task = open('/tmp/autonomy_task.md', 'r').read()
    
    system_prompt = """You are kael, an autonomous AI worker. You can use the following tools:

1. read_file(path) - Read file
2. write_file(path, content) - Write file  
3. shell_exec(command) - Execute shell command
4. search_files(pattern, directory) - Search for files

Tool call format:
```json
{"tool": "tool_name", "params": {"param_name": "param_value"}}
```

Important: You can call multiple tools consecutively. After each tool call, I will tell you the result, and you continue reasoning and calling the next tool.
Do not stop until the task is complete."""

    messages = [
        {'role': 'user', 'content': task},
    ]
    
    logger.info("=== Test 1: Direct LLM Call ===")
    logger.info(f"System prompt length: {len(system_prompt)}")
    logger.info(f"Task length: {len(task)}")
    
    # Round 1
    start = time.time()
    result = await llm.chat(messages=messages, system_prompt=system_prompt, stream=False)
    elapsed = time.time() - start
    content = result.get('content', '')
    logger.info(f"Round 1 (elapsed {elapsed:.1f}s):")
    logger.info(f"AI response ({len(content)} chars):\n{content[:2000]}")
    
    # Check if AI made tool calls
    from tical_code.core.tool_call_parser import parse_tool_calls
    tool_calls = parse_tool_calls(content)
    logger.info(f"Parsed {len(tool_calls)} tool calls: {[tc.get('tool') for tc in tool_calls]}")
    
    return content, tool_calls


async def test_workerloop_bypass_clarify():
    """Test 2: Use WorkerLoop but disable Clarify engine"""
    from tical_code.core.worker_framework import WorkerFramework, WorkerConfig
    from tical_code.core.worker_loop import WorkerLoop, WorkerLoopConfig, UserMessage
    
    config = WorkerConfig.from_file(str(PROJECT_ROOT / 'config' / 'worker-configs' / 'kael-local.json'))
    worker = WorkerFramework(config)
    await worker.bootstrap()
    
    # Disable clarify - directly set flag
    worker._has_clarify = False
    
    task = open('/tmp/autonomy_task.md', 'r').read()
    
    logger.info("=== Test 2: WorkerLoop (Disable Clarify) ===")
    
    start = time.time()
    try:
        response = await worker.handle_message('autonomy_v2', task)
        elapsed = time.time() - start
        logger.info(f"AI response (elapsed {elapsed:.1f}s, {len(response)} chars):\n{response[:2000]}")
        
        # Check session history
        if worker.worker_loop:
            history = worker.worker_loop.get_session_history('autonomy_v2')
            logger.info(f"Session history message count: {len(history)}")
            for i, msg in enumerate(history):
                role = msg.get('role', '?')
                content_preview = msg.get('content', '')[:150]
                logger.info(f"  [{i}] {role}: {content_preview}")
        
        return response
    except Exception as e:
        elapsed = time.time() - start
        logger.error(f"handle_message exception (elapsed {elapsed:.1f}s): {e}")
        import traceback
        traceback.print_exc()
        return str(e)


async def test_manual_tool_loop():
    """Test 3: Manual simulated autonomy loop - repeatedly call LLM + execute tools"""
    from tical_code.core.llm_interface import LLMInterface
    from tical_code.core.tool_call_parser import parse_tool_calls, format_tool_result
    from tical_code.core.worker_framework import WorkerFramework, WorkerConfig
    
    # Initialize framework to get tool execution capability
    config = WorkerConfig.from_file(str(PROJECT_ROOT / 'config' / 'worker-configs' / 'kael-local.json'))
    worker = WorkerFramework(config)
    await worker.bootstrap()
    
    llm = LLMInterface({'default_model': 'mimo-v2-pro'})
    
    task = open('/tmp/autonomy_task.md', 'r').read()
    
    system_prompt = f"""You are kael, an autonomous AI worker. Your task is to autonomously complete programming work.

You can use the following tools:
1. read_file(path) - Read file contents
2. write_file(path, content) - Write file
3. shell_exec(command) - Execute shell command
4. search_files(pattern, directory) - Search for files
5. patch_file(path, old_content, new_content) - Modify file

Tool call format (one per line):
```json
{{"tool": "tool_name", "params": {{"param_name": "param_value"}}}}
```

Key rules:
- You must continue working until the task is complete
- Tool execution results will be automatically returned to you, you should continue reasoning
- Don't just analyze, actually execute tool calls
- Say "TASK_COMPLETE" after completing the task """
    
    messages = [
        {'role': 'user', 'content': task},
    ]
    
    logger.info("=== Test 3: Manual Autonomy Loop ===")
    
    # Record each step
    steps = []
    max_iterations = 10
    
    for iteration in range(1, max_iterations + 1):
        logger.info(f"\n--- Iteration {iteration} ---")
        
        # Call LLM
        start = time.time()
        try:
            result = await llm.chat(messages=messages, system_prompt=system_prompt, stream=False)
            elapsed = time.time() - start
        except Exception as e:
            logger.error(f"LLM call failed: {e}")
            steps.append({
                'iteration': iteration,
                'error': str(e),
                'elapsed': time.time() - start,
            })
            break
        
        content = result.get('content', '')
        logger.info(f"LLM response (elapsed {elapsed:.1f}s, {len(content)} chars):")
        logger.info(content[:1500])
        
        # Parse tool calls
        tool_calls = parse_tool_calls(content)
        logger.info(f"Parsed {len(tool_calls)} tool calls: {[tc.get('tool') for tc in tool_calls]}")
        
        # Record step
        steps.append({
            'iteration': iteration,
            'content_length': len(content),
            'tool_calls': len(tool_calls),
            'tool_names': [tc.get('tool') for tc in tool_calls],
            'elapsed': elapsed,
            'content_preview': content[:300],
        })
        
        # If no tool calls, check if task is complete
        if not tool_calls:
            if 'TASK_COMPLETE' in content:
                logger.info("Task complete!")
            else:
                logger.info("No tool calls and task not complete - AI stopped proactively")
            break
        
        # Execute tools
        tool_results = []
        for tc in tool_calls:
            tool_name = tc.get('tool', 'unknown')
            params = tc.get('params', {})
            logger.info(f"  Executing tool: {tool_name}({params})")
            
            # Use WorkerLoop's tool execution capability
            try:
                from tical_code.core.worker_loop import ToolExecutionResult
                result_obj = await worker.worker_loop._execute_single_tool(tc)
                tool_result_text = format_tool_result(result_obj.tool_name, result_obj.data, result_obj.error)
                logger.info(f"  Tool result: {tool_result_text[:200]}")
                tool_results.append(tool_result_text)
            except Exception as e:
                error_text = f"Tool execution error: {e}"
                logger.error(f"  {error_text}")
                tool_results.append(error_text)
        
        # Add result to messages, continue loop
        messages.append({'role': 'assistant', 'content': content})
        for tr in tool_results:
            messages.append({'role': 'user', 'content': f"Tool execution results:\n{tr}"})
        
        # Prevent messages from being too long
        if len(messages) > 20:
            # Keep system prompt context + recent messages
            messages = messages[-16:]
    
    # Save step record
    report_path = '/tmp/autonomy_test_steps.json'
    with open(report_path, 'w', encoding='utf-8') as f:
        json.dump(steps, f, ensure_ascii=False, indent=2)
    logger.info(f"\nStep record saved to {report_path}")
    
    return steps


async def main():
    # Test 1: Direct LLM
    try:
        await test_direct_llm()
    except Exception as e:
        logger.error(f"Test 1 failed: {e}")
        import traceback
        traceback.print_exc()
    
    print("\n" + "=" * 80 + "\n")
    
    # Test 2: WorkerLoop (Disable Clarify)
    try:
        await test_workerloop_bypass_clarify()
    except Exception as e:
        logger.error(f"Test 2 failed: {e}")
        import traceback
        traceback.print_exc()
    
    print("\n" + "=" * 80 + "\n")
    
    # Test 3: Manual Autonomy Loop
    try:
        steps = await test_manual_tool_loop()
        logger.info(f"\nTotal iterations: {len(steps)}")
        for step in steps:
            logger.info(f"  Iteration {step.get('iteration', '?')}: "
                       f"tools={step.get('tool_calls', 0)}, "
                       f"names={step.get('tool_names', [])}, "
                       f"time={step.get('elapsed', 0):.1f}s")
    except Exception as e:
        logger.error(f"Test 3 failed: {e}")
        import traceback
        traceback.print_exc()


if __name__ == '__main__':
    asyncio.run(main())

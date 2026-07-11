#!/usr/bin/env python3
"""
Phase 1: Let AI worker self-fix 5 autonomous loop issues
Observe AI capabilities: can it understand problems, locate code, write fixes
"""
import asyncio
import logging
import os
import sys
import time
import json
from pathlib import Path

PROJECT_ROOT = Path('/app/data/all_conversations/main_conversation/tical-code-v0.3')
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

os.environ['MIMO_API_KEY'] = '${MIMO_API_KEY}'

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
)
logger = logging.getLogger('phase1_ai_selffix')


async def run_ai_selffix():
    """Let AI self-fix 5 issues - using manual loop to bypass Clarify"""
    from tical_code.core.llm_interface import LLMInterface
    from tical_code.core.tool_call_parser import parse_tool_calls, format_tool_result
    from tical_code.core.worker_framework import WorkerFramework, WorkerConfig
    from tical_code.core.builtin_tools import (
        read_file_handler, write_file_handler, shell_exec_handler,
        search_files_handler, patch_file_handler,
        set_security_context, SecurityContext,
    )
    
    # Set security context to allow project directory
    sec_ctx = SecurityContext(allowed_dirs=['~', '/app', '/tmp', '/root'])
    set_security_context(sec_ctx)
    
    config = WorkerConfig.from_file(str(PROJECT_ROOT / 'config' / 'worker-configs' / 'kael-local.json'))
    worker = WorkerFramework(config)
    await worker.bootstrap()
    
    llm = LLMInterface({'default_model': 'mimo-v2-pro'})
    
    # Read task
    task = open(str(PROJECT_ROOT / 'fix_autonomy_issues.md'), 'r').read()
    
    system_prompt = """You are kael, an autonomous AI worker running on tical-code v0.5.8.

Your core capability is autonomous programming - no human guidance needed, you read code, design, write code, and test on your own.

Available tools (note parameter names):
1. read_file(path, offset, limit) - Read file contents, path=file path, offset=starting line number, limit=line count
2. write_file(path, content, append) - Write file, path=file path, content=file contents, append=whether to append
3. shell_exec(cmd) - Execute shell command, cmd=command string (note: parameter name is cmd, not command)
4. search_files(pattern, directory) - Search files, pattern=file name pattern, directory=search directory
5. patch_file(path, find, replace) - Modify file, path=file path, find=text to find, replace=replacement text (note: parameter name is find, not old_content)

Tool call format:
```json
{"tool": "tool_name", "params": {"param_name": "param_value"}}
```

Key requirements:
- You must keep working until the task is complete
- After reading code, write code - don't just read without writing
- Call a tool at every step - no empty talk
- Prioritize patch_file for code changes (safer than write_file which rewrites the entire file)
- Output TASK_COMPLETE when done
- On failure, retry or change approach
- Do not repeatedly read the same file"""

    messages = [{'role': 'user', 'content': task}]
    
    logger.info("=" * 60)
    logger.info("Phase 1: AI autonomous fix test start")
    logger.info("=" * 60)
    
    steps = []
    max_iterations = 15
    consecutive_no_tools = 0
    read_count = 0
    write_count = 0
    phase_log = []  # Record what each step does
    
    for iteration in range(1, max_iterations + 1):
        logger.info(f"\n{'='*40}")
        logger.info(f"Iteration {iteration}/{max_iterations}")
        logger.info(f"{'='*40}")
        
        # Check if stuck in read-only loop
        if read_count >= 5 and write_count == 0:
            hint = f"\n\n⚠️ Reminder: You have read code {read_count} times but haven't modified any files! Please immediately use patch_file or write_file to start modifying code!"
            messages[-1]['content'] = messages[-1]['content'] + hint
            logger.info(f"💡 Inject analysis paralysis hint (reads={read_count}/writes={write_count})")
        
        start = time.time()
        try:
            result = await llm.chat(messages=messages, system_prompt=system_prompt, stream=False)
            elapsed = time.time() - start
        except Exception as e:
            elapsed = time.time() - start
            logger.error(f"LLM call failed (elapsed {elapsed:.1f}s): {e}")
            steps.append({'iteration': iteration, 'error': str(e), 'elapsed': elapsed})
            if 'timed out' in str(e).lower() or 'timeout' in str(e).lower():
                logger.info("Timeout, waiting 5 seconds before retry...")
                await asyncio.sleep(5)
                continue
            break
        
        content = result.get('content', '')
        logger.info(f"AI response (elapsed {elapsed:.1f}s, {len(content)} chars):")
        # Print first 1500 chars
        logger.info(content[:1500])
        if len(content) > 1500:
            logger.info(f"... (omitted {len(content)-1500} chars)")
        
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
                logger.info("✅ AI reports task complete!")
                phase_log.append(f"Iteration {iteration}: AI reports TASK_COMPLETE")
                break
            if consecutive_no_tools >= 2:
                logger.info("⚠️ 2 consecutive rounds without tool calls, AI stopped")
                phase_log.append(f"Iteration {iteration}: no consecutive tool calls, AI stopped")
                break
        else:
            consecutive_no_tools = 0
        
        # Execute tools
        tool_results = []
        for tc in tool_calls:
            tool_name = tc.get('tool', 'unknown')
            params = tc.get('params', {})
            params_str = json.dumps(params, ensure_ascii=False)
            if len(params_str) > 200:
                params_str = params_str[:200] + "..."
            logger.info(f"  Exec: {tool_name}({params_str})")
            
            # Track reads/writes
            if tool_name in ('read_file', 'search_files', 'list_dir'):
                read_count += 1
                phase_log.append(f"Iteration {iteration}: READ - {tool_name}({params_str[:80]})")
            elif tool_name in ('write_file', 'patch_file', 'shell_exec'):
                write_count += 1
                read_count = 0  # Write operation resets read count
                phase_log.append(f"Iteration {iteration}: WRITE - {tool_name}({params_str[:80]})")
            
            try:
                result_obj = await _execute_tool(worker, tc)
                result_text = format_tool_result(result_obj.get('tool_name', tool_name), result_obj.get('data'), result_obj.get('error'))
                # Truncate overly long results
                if len(result_text) > 3000:
                    result_text = result_text[:3000] + f"\n... (truncated, original length {len(result_text)})"
                logger.info(f"  Result ({len(result_text)} chars): {result_text[:300]}")
                tool_results.append(result_text)
            except Exception as e:
                error_text = f"Tool execution exception: {e}"
                logger.error(f"  {error_text}")
                import traceback
                traceback.print_exc()
                tool_results.append(error_text)
        
        # Add results to conversation
        if tool_results:
            combined = "\n\n".join(tool_results)
            messages.append({'role': 'assistant', 'content': content})
            messages.append({'role': 'user', 'content': f"Tool execution results:\n{combined}\n\nPlease continue working."})

        logger.info(f"📊 Status: reads={read_count}/writes={write_count}, msg_count={len(messages)}")
    
    # Output summary
    logger.info("\n" + "=" * 60)
    logger.info("Phase 1 Summary")
    logger.info("=" * 60)
    logger.info(f"Total iterations: {len(steps)}")
    logger.info(f"Total reads/writes: {read_count}/{write_count}")
    logger.info(f"Execution log:")
    for log_entry in phase_log:
        logger.info(f"  {log_entry}")
    
    # Save execution log to file
    report = {
        'total_iterations': len(steps),
        'total_reads': read_count,
        'total_writes': write_count,
        'steps': steps,
        'phase_log': phase_log,
    }
    with open('/tmp/phase1_ai_selffix_report.json', 'w') as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    logger.info("Execution log saved to /tmp/phase1_ai_selffix_report.json")
    
    await worker.shutdown(skip_death_record=True)
    return report


async def _execute_tool(worker, tool_call: dict) -> dict:
    """Execute a single tool call, return result dict"""
    tool_name = tool_call.get('tool', 'unknown')
    params = tool_call.get('params', {})
    
    if tool_name == 'read_file':
        result = read_file_handler(
            path=params.get('path', ''),
            offset=params.get('offset', 0),
            limit=params.get('limit', 100),
        )
        return {'tool_name': tool_name, 'data': result, 'error': result.get('error')}
    
    elif tool_name == 'write_file':
        result = write_file_handler(
            path=params.get('path', ''),
            content=params.get('content', ''),
            append=params.get('append', False),
        )
        return {'tool_name': tool_name, 'data': result, 'error': result.get('error')}
    
    elif tool_name == 'shell_exec':
        result = shell_exec_handler(
            cmd=params.get('cmd', params.get('command', '')),
            timeout=params.get('timeout', 10),
        )
        return {'tool_name': tool_name, 'data': result, 'error': result.get('error')}
    
    elif tool_name == 'search_files':
        result = search_files_handler(
            pattern=params.get('pattern', params.get('file_pattern', '')),
            directory=params.get('directory', '.'),
        )
        return {'tool_name': tool_name, 'data': result, 'error': result.get('error')}
    
    elif tool_name == 'patch_file':
        result = await patch_file_handler(
            params={
                'path': params.get('path', params.get('file_path', '')),
                'find': params.get('find', params.get('old_content', '')),
                'replace': params.get('replace', params.get('new_content', '')),
                'count': params.get('count', 1),
                'backup': params.get('backup', True),
            },
            context={},
        )
        return {'tool_name': tool_name, 'data': result.to_dict() if hasattr(result, 'to_dict') else result, 'error': getattr(result, 'error', None)}
    
    elif tool_name == 'list_dir':
        from tical_code.core.builtin_tools import list_dir_handler
        result = list_dir_handler(
            path=params.get('path', '.'),
            all_files=params.get('all', False),
        )
        return {'tool_name': tool_name, 'data': result, 'error': result.get('error')}
    
    else:
        return {'tool_name': tool_name, 'data': None, 'error': f'Unknown tool: {tool_name}'}


if __name__ == '__main__':
    report = asyncio.run(run_ai_selffix())
    print(f"\n{'='*60}")
    print(f"Phase 1 complete: {report['total_iterations']} rounds, reads={report['total_reads']}/writes={report['total_writes']}")
    print(f"{'='*60}")

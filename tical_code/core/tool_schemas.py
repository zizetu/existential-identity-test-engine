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
# Original repository: https://github.com/zizetu/tical-agent
#

"""Tool schemas for tical-code agent — OpenAI function-calling definitions.

Extracted from tool_executor.py to reduce module size (~550 lines → 20 lines).
Import path: from tical_code.core.tool_schemas import TOOL_SCHEMAS, TOOL_SCHEMAS_CLEAN
Re-exported via tool_executor for backward compatibility.
"""

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "shell_exec",
            "description": "Execute shell commands (safety-checked). Use for file operations, system management, and network requests. Set workdir instead of using cd.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command to execute"},
                    "workdir": {"type": "string", "description": "Optional working directory. Set this instead of using 'cd' in the command."}
                },
                "required": ["command"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "file_read",
            "description": "Read file content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path"}
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "file_write",
            "description": "Write content to a file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path"},
                    "content": {"type": "string", "description": "Content to write"}
                },
                "required": ["path", "content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "file_patch",
            "description": "Find and replace text in a file. Use for targeted edits instead of rewriting the whole file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path to edit"},
                    "old_string": {"type": "string", "description": "Text to find (include surrounding context for uniqueness)"},
                    "new_string": {"type": "string", "description": "Replacement text. Pass empty string to delete the matched text."}
                },
                "required": ["path", "old_string", "new_string"]
            }
        }
    },

    {
        "type": "function",
        "function": {
            "name": "memory_save",
            "description": "Save a piece of persistent memory to file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "Memory key name"},
                    "value": {"type": "string", "description": "Memory value"}
                },
                "required": ["key", "value"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "memory_load",
            "description": "Read all saved persistent memories.",
            "parameters": {
                "type": "object",
                "properties": {}
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "memory_search",
            "description": "Full-text search across all conversations and memory documents (SOUL.md, MEMORY.md, USER.md). Uses FTS5 with CJK-aware tokenization.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query (keywords or phrase)"},
                    "limit": {"type": "integer", "description": "Max results (default 10)"}
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "memory",
            "description": "Manage persistent memory: store, recall, search, or forget entries.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["store", "recall", "search", "forget"],
                        "description": "Memory action: store a new entry, recall by key, search all entries, or forget/delete an entry."
                    },
                    "key": {
                        "type": "string",
                        "description": "Memory key to recall or forget. Required for recall and forget actions."
                    },
                    "value": {
                        "type": "string",
                        "description": "Content to store. Required for the store action."
                    },
                    "query": {
                        "type": "string",
                        "description": "Search query for the search action."
                    }
                },
                "required": ["action"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "state_save",
            "description": "Save persistent state (non-memory key-value data).",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "State key name"},
                    "value": {"type": "object", "description": "State value (JSON object)"}
                },
                "required": ["key", "value"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "chat_send",
            "description": "Send a message to another AI worker via tical-chat, or reply to the user. Prefer end_task for task completion.",
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {"type": "string", "description": "Target AI worker identity"},
                    "content": {"type": "string", "description": "Message content"}
                },
                "required": ["target", "content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "restart_self",
            "description": "Restart this worker process. Sends SIGTERM -- systemd auto-restarts cleanly. Use to clear long-running context or after config changes.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "web_fetch",
            "description": "Fetch a URL and return the content as readable text. Has SSRF protection (blocks private IPs).",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL to fetch (http/https only)"},
                    "timeout": {"type": "integer", "description": "Timeout in seconds (default 10, max 30)"}
                },
                "required": ["url"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "file_search",
            "description": "Search for files by name pattern or content. Uses glob patterns for filenames.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Glob pattern for file names, e.g. *.py, *config*"},
                    "directory": {"type": "string", "description": "Directory to search in (default: current workspace)"},
                    "content_pattern": {"type": "string", "description": "Optional text to search inside files"}
                },
                "required": ["pattern"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List directory contents with file size and modification time metadata.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Directory path to list (default: current directory)"},
                    "all": {"type": "boolean", "description": "Include hidden files (default: false)"}
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "check_self",
            "description": "Check own runtime info: model, config, identity. Always use this when asked about your model or capabilities.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "verify_multi",
            "description": "Send the same prompt to multiple AI models, compare answers, and produce a consensus audit. Use BEFORE high-stakes actions (file writes, deployments). Returns divergence score (0=unanimous, 1=completely divergent).",
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "The prompt or question to verify across models."},
                    "threshold": {"type": "number", "description": "Divergence threshold above which action is blocked (default 0.3)."}
                },
                "required": ["prompt"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "http_post",
            "description": "POST data to a URL. Use for API calls and webhooks.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL to POST to"},
                    "data": {"type": "string", "description": "POST body"},
                    "content_type": {"type": "string", "description": "Content-Type (default: application/json)"},
                    "timeout": {"type": "integer", "description": "Timeout in seconds (default 10, max 30)"},
                },
                "required": ["url", "data"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "delegate_task",
            "description": "Delegate a task to a sub-agent for parallel execution. The sub-agent runs independently with its own session and tools. Returns a task_id for result retrieval.",
            "parameters": {
                "type": "object",
                "properties": {
                    "description": {"type": "string", "description": "Task description for the sub-agent to execute"},
                    "tools": {"type": "array", "items": {"type": "string"}, "description": "Tool names available to the sub-agent (default: all tools)"},
                    "max_iterations": {"type": "integer", "description": "Maximum reasoning rounds (default: 5)"}
                },
                "required": ["description"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_subagent_result",
            "description": "Retrieve the result of a previously delegated sub-agent task using the task_id from delegate_task. Status can be pending, running, or complete.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "The task_id returned from delegate_task"}
                },
                "required": ["task_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "vigil_status",
            "description": "Query Vigil's current state: patrol count, human/ai state, recent verdicts, and pending instructions.",
            "parameters": {
                "type": "object",
                "properties": {}
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "check_metrics",
            "description": "Return performance metrics: tool latency averages, LLM call latency, error counts per tool, and top 5 slowest tool calls.",
            "parameters": {
                "type": "object",
                "properties": {}
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "end_task",
            "description": "Signal that the current task is complete. Call when all work is done. Triggers memory consolidation.",
            "parameters": {
                "type": "object",
                "properties": {
                    "success": {"type": "boolean", "description": "Whether the task succeeded"}
                },
                "required": ["success"]
            }
        },
    },
    {
        "type": "function",
        "function": {
            "name": "chain_exec",
            "description": (
                "Execute a molecular chain - a sequence of AI models where each "
                "model's output feeds into the next, producing emergent intelligence. "
                "Supports preset chains and dynamic chains. The engine auto-routes "
                "each step to the best provider: local small models for structured "
                "tasks, cloud API for creative tasks, distillate model for user-"
                "aligned judgments."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "molecule": {
                        "type": "string",
                        "description": "Which preset chain to execute.",
                        "enum": ["code_review", "research",
                                 "safety_check", "decision"],
                    },
                    "prompt": {
                        "type": "string",
                        "description": "The input prompt for the molecular chain.",
                    },
                    "context": {
                        "type": "string",
                        "description": "Optional context to include in each step.",
                    },
                    "custom_steps": {
                        "type": "array",
                        "description": (
                            "Custom chain steps. role options: reasoner, executor, "
                            "verifier, guard, synthesizer, formatter, distillate, "
                            "translator, summarizer, classifier, retriever, "
                            "cryptograph, compliance, or any custom role."
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "role": {"type": "string"},
                                "prompt_template": {"type": "string"},
                                "provider_type": {
                                    "type": "string",
                                    "enum": ["auto", "api", "local", "distillate"],
                                    "default": "auto",
                                },
                                "bond_type": {
                                    "type": "string",
                                    "enum": ["refine", "verify", "transform",
                                             "catalyze"],
                                    "default": "refine",
                                },
                            },
                            "required": ["role", "prompt_template"],
                        },
                    },
                    "provider_preference": {
                        "type": "string",
                        "enum": ["auto", "prefer_local", "prefer_api", "local_only"],
                        "default": "auto",
                    },
                },
                "required": ["prompt"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "safe_modify",
            "description": (
                "Safely modify a file with full safety checks: protected file check, "
                "git backup, syntax validation, code safety check, sandbox test, "
                "cross-verify, and audit logging. Automatically rolls back on failure. "
                "USE THIS instead of file_write for modifying system code to prevent "
                "breaking the worker."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path to modify"},
                    "new_content": {"type": "string", "description": "New complete file content"},
                    "reason": {"type": "string", "description": "Human-readable reason for this modification. Be specific: what bug/feature, why this change."},
                    "sandbox_test": {"type": "boolean", "description": "Run sandbox test after write (default: true)"},
                },
                "required": ["path", "new_content", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "safe_modify_diff",
            "description": (
                "Apply a targeted find-and-replace through the safe_modify pipeline "
                "(safety checks + rollback). Reads file, applies diff, validates. "
                "USE THIS instead of file_patch for system code edits."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path to edit"},
                    "old_string": {"type": "string", "description": "Text to find (include surrounding context for uniqueness)"},
                    "new_string": {"type": "string", "description": "Replacement text. Pass empty string to delete."},
                    "reason": {"type": "string", "description": "Human-readable reason for this modification."},
                },
                "required": ["path", "old_string", "new_string", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "checkpoint_list",
            "description": (
                "List all available checkpoints/snapshots with status filter. "
                "Returns checkpoints with id, timestamp, description, status, and file count. "
                "Use this before checkpoint_restore to find the right checkpoint ID."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "description": "Optional status filter: 'incomplete', 'complete', or omit for all",
                        "enum": ["incomplete", "complete"],
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "checkpoint_restore",
            "description": (
                "Restore files from a checkpoint/snapshot. Automatically creates a pre-snapshot "
                "before restoring for safety. Requires confirm=True to execute - use preview first "
                "by calling without confirm to see what files will be affected."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "checkpoint_id": {"type": "string", "description": "Checkpoint ID to restore from"},
                    "selective_files": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional list of specific file paths to restore (omit for full restore)",
                    },
                    "confirm": {
                        "type": "boolean",
                        "description": "Must be True to proceed. Call without confirm first to preview.",
                        "default": False,
                    },
                },
                "required": ["checkpoint_id"],
            },
        },
    },
    # Capability integration tools (auto-discovered)
    {
        "type": "function",
        "function": {
            "name": "capability_list",
            "description": (
                "List all system capabilities. Returns a manifest of every module "
                "and what it can do. Use this to discover what capabilities your "
                "system has beyond the standard tools."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "capability_call",
            "description": (
                "Invoke a system capability by name. Use capability_list first to "
                "see what's available. Call format: pass name and params."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Capability name from capability_list",
                    },
                    "params": {
                        "type": "object",
                        "description": "Parameters for the capability",
                    },
                },
                "required": ["name"],
            },
        },
    },
    # start_background_task: persist a multi-step plan for autonomous execution
    {
        "type": "function",
        "function": {
            "name": "start_background_task",
            "description": (
                "Create a persistent autonomous task that runs in the background. "
                "Use this for any work that will take more than 3-5 tool calls - "
                "the task engine will continue executing step by step across multiple "
                "LLM rounds until completion or failure. "
                "Output is sent back via chat_send as the task progresses. "
                "Call this tool with a clear goal and optional step-by-step plan, "
                "then call end_task to signal the current message turn is done. "
                "The background worker picks up the task on the next loop iteration."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "goal": {
                        "type": "string",
                        "description": "The overall task goal - what you want to accomplish",
                    },
                    "plan": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional step-by-step plan. Each item is one step.",
                    },
                    "max_steps": {
                        "type": "integer",
                        "description": "Maximum LLM rounds before forced completion (default: 100)",
                        "default": 100,
                    },
                },
                "required": ["goal"],
            },
        },
    },
    # ask_user: pause and ask the human for input
    {
        "type": "function",
        "function": {
            "name": "ask_user",
            "description": (
                "Ask the human user for input when you are stuck, need a CAPTCHA code, "
                "need confirmation, or cannot proceed with the current task. "
                "Use this instead of trying the same thing repeatedly."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "The question to ask the user. Be specific about what you need.",
                    },
                    "context": {
                        "type": "string",
                        "description": "Optional context explaining why you need this input (e.g., 'CAPTCHA detected on login page', 'need confirmation to proceed')",
                    },
                },
                "required": ["question"],
            },
        },
    },
    # file_state_list: query files touched in current task
    {
        "type": "function",
        "function": {
            "name": "file_state_list",
            "description": "List all files touched in the current task with their action, size, and timestamp. Use to avoid re-reading files already in context.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },

    # LIVE WIRE 2026-07-09f: sustained task tools (SustainedTaskManager)
    {
        "type": "function",
        "function": {
            "name": "task_create",
            "description": "Create a durable multi-step task that survives restarts. Use for long work that must continue later.",
            "parameters": {
                "type": "object",
                "properties": {
                    "goal": {"type": "string", "description": "Task goal / description"},
                    "context": {"type": "string", "description": "Optional context JSON/text"},
                    "max_retries": {"type": "integer", "description": "Max retries (default 3)", "default": 3}
                },
                "required": ["goal"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "task_list",
            "description": "List durable sustained tasks (newest first).",
            "parameters": {
                "type": "object",
                "properties": {
                    "state": {"type": "string", "description": "Optional filter: pending|running|completed|failed|cancelled"},
                    "limit": {"type": "integer", "default": 20}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "task_status",
            "description": "Get status of one sustained task by task_id.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "Task id from task_create"}
                },
                "required": ["task_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "task_cancel",
            "description": "Cancel a sustained task by task_id.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"}
                },
                "required": ["task_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "evolve_stats",
            "description": "Show self-evolution stats: frequent errors, suggestions, usage insights.",
            "parameters": {"type": "object", "properties": {}}
        }
    },
]

# Clean alias — all tool names already use underscores
TOOL_SCHEMAS_CLEAN = TOOL_SCHEMAS

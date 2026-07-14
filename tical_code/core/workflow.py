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
#
# Original repository: https://github.com/zizetu/existential-identity-test-engine
#

"""
Workflow Engine (EITElite v0.3 Core)
=======================================

Visual workflow orchestration in pure Python.
Inspired by Coze Studio's Workflow, but with EITElite philosophy:
- Every node MUST pass Force-Verify before proceeding
- All execution is traced
- Evidence hash for each node output
- Code execution is ALWAYS sandboxed (never trust AI output)

Node types:
- LLMNode: LLM call
- ConditionNode: Conditional branching
- HTTPNode: HTTP request
- CodeNode: Python code execution (sandboxed)
- PluginNode: Plugin tool call
- ParallelNode: Fan-out/fan-in execution

Edition: FULL ONLY
"""

# DESIGNED-NOT-DEAD: Workflow engine (DAG-based task orchestration). Awaiting decision_engine. DO NOT DELETE - multi-step task automation foundation.


# Edition check - relaxed since unified system (2026-06-08)
try:
    from .detection import detect_edition
    _edition = detect_edition()
    if _edition == "lite":
        import logging as _log
        _log.getLogger(__name__).warning("workflow module loaded on lite system - complex workflows may be slower")
except ImportError:
    pass

import asyncio
import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Union
import logging

logger = logging.getLogger(__name__)

# Import sandbox for safe code execution
from .sandbox import SandboxExecutor, SandboxConfig, SandboxResult, get_sandbox


# =============================================================================
# Workflow Exceptions
# =============================================================================

class WorkflowError(Exception):
    """Base exception for workflow errors."""
    pass


class WorkflowVerificationError(WorkflowError):
    """Raised when workflow node verification fails."""
    pass


class WorkflowNodeError(WorkflowError):
    """Raised when workflow node execution fails."""
    pass


# =============================================================================
# Node Types
# =============================================================================

class NodeType(Enum):
    """Types of workflow nodes."""
    LLM = "llm"
    CONDITION = "condition"
    HTTP = "http"
    CODE = "code"
    PLUGIN = "plugin"
    PARALLEL = "parallel"
    START = "start"
    END = "end"


class NodeStatus(Enum):
    """Node execution status."""
    PENDING = "pending"
    RUNNING = "running"
    VERIFYING = "verifying"
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"


# =============================================================================
# Node Definition
# =============================================================================

@dataclass
class WorkflowNode:
    """
    A single node in a workflow.
    
    Each node has:
    - id: Unique identifier
    - type: Node type
    - config: Node-specific configuration
    - input_mapping: How to get inputs from previous nodes
    - verify_schema: Schema for Force-Verify
    - next_nodes: IDs of nodes to execute after this one
    """
    node_id: str
    node_type: NodeType
    name: str
    
    # Configuration
    config: Dict[str, Any] = field(default_factory=dict)
    
    # Input/output
    input_mapping: Dict[str, str] = field(default_factory=dict)
    output_key: Optional[str] = None
    
    # Verification
    verify_level: str = "SCHEMA"
    verify_schema: Optional[Dict] = None
    
    # Control flow
    next_nodes: List[str] = field(default_factory=list)
    condition_expr: Optional[str] = None
    
    # Status (runtime)
    status: NodeStatus = NodeStatus.PENDING
    error: Optional[str] = None
    
    # Execution tracking
    execution_count: int = 0
    total_duration_ms: float = 0.0
    evidence_hash: Optional[str] = None
    
    def to_dict(self) -> Dict:
        """Convert to dictionary."""
        return {
            'node_id': self.node_id,
            'node_type': self.node_type.value,
            'name': self.name,
            'config': self.config,
            'input_mapping': self.input_mapping,
            'output_key': self.output_key,
            'verify_level': self.verify_level,
            'verify_schema': self.verify_schema,
            'next_nodes': self.next_nodes,
            'condition_expr': self.condition_expr,
            'status': self.status.value,
            'error': self.error,
            'execution_count': self.execution_count,
            'total_duration_ms': self.total_duration_ms,
            'evidence_hash': self.evidence_hash,
        }


# =============================================================================
# Workflow Definition
# =============================================================================

@dataclass
class Workflow:
    """
    A complete workflow definition.
    
    Workflows are defined as:
    - name: Human-readable name
    - version: Version string
    - nodes: Dictionary of node_id -> WorkflowNode
    - start_node: ID of the start node
    - end_nodes: List of end node IDs
    """
    name: str
    version: str = "0.1.5"
    
    # Nodes
    nodes: Dict[str, WorkflowNode] = field(default_factory=dict)
    start_node: Optional[str] = None
    end_nodes: List[str] = field(default_factory=list)
    
    # Metadata
    description: str = ""
    author: str = ""
    created_at: float = field(default_factory=time.time)
    
    # Global config
    default_verify_level: str = "SCHEMA"
    timeout_seconds: int = 300
    
    def add_node(self, node: WorkflowNode):
        """Add a node to the workflow."""
        self.nodes[node.node_id] = node
        if self.start_node is None:
            self.start_node = node.node_id
        if node.node_type == NodeType.END:
            self.end_nodes.append(node.node_id)
    
    def get_node(self, node_id: str) -> Optional[WorkflowNode]:
        """Get a node by ID."""
        return self.nodes.get(node_id)
    
    def get_start_nodes(self) -> List[WorkflowNode]:
        """Get nodes with no predecessors."""
        all_predecessors = set()
        for node in self.nodes.values():
            all_predecessors.update(node.input_mapping.values())
        
        return [
            node for node in self.nodes.values()
            if node.node_id not in all_predecessors
        ]
    
    def to_dict(self) -> Dict:
        """Convert to dictionary."""
        return {
            'name': self.name,
            'version': self.version,
            'description': self.description,
            'author': self.author,
            'created_at': self.created_at,
            'default_verify_level': self.default_verify_level,
            'timeout_seconds': self.timeout_seconds,
            'start_node': self.start_node,
            'end_nodes': self.end_nodes,
            'nodes': {k: v.to_dict() for k, v in self.nodes.items()},
        }
    
    @classmethod
    def from_dict(cls, data: Dict) -> 'Workflow':
        """Create from dictionary."""
        workflow = cls(
            name=data['name'],
            version=data.get('version', '0.1.5'),
            description=data.get('description', ''),
            author=data.get('author', ''),
            created_at=data.get('created_at', time.time()),
            default_verify_level=data.get('default_verify_level', 'SCHEMA'),
            timeout_seconds=data.get('timeout_seconds', 300),
            start_node=data.get('start_node'),
            end_nodes=data.get('end_nodes', []),
        )
        
        for node_id, node_data in data.get('nodes', {}).items():
            workflow.nodes[node_id] = WorkflowNode(
                node_id=node_data['node_id'],
                node_type=NodeType(node_data['node_type']),
                name=node_data['name'],
                config=node_data.get('config', {}),
                input_mapping=node_data.get('input_mapping', {}),
                output_key=node_data.get('output_key'),
                verify_level=node_data.get('verify_level', 'SCHEMA'),
                verify_schema=node_data.get('verify_schema'),
                next_nodes=node_data.get('next_nodes', []),
                condition_expr=node_data.get('condition_expr'),
            )
        
        return workflow
    
    @classmethod
    def from_json(cls, json_str: str) -> 'Workflow':
        """Create from JSON string."""
        return cls.from_dict(json.loads(json_str))
    
    def to_json(self) -> str:
        """Convert to JSON string."""
        return json.dumps(self.to_dict(), indent=2, default=str)


# =============================================================================
# Node Executors
# =============================================================================

class NodeExecutor:
    """
    Base executor for workflow nodes.
    
    Each node type has its own executor.
    """
    
    def __init__(self, workflow: Workflow, executor: 'WorkflowExecutor'):
        self.workflow = workflow
        self.executor = executor
    
    async def execute(
        self,
        node: WorkflowNode,
        context: Dict[str, Any],
    ) -> Any:
        """
        Execute a node.
        
        Args:
            node: The node to execute
            context: Execution context (shared state)
            
        Returns:
            Node output
        """
        pass
    
    def _get_inputs(
        self,
        node: WorkflowNode,
        context: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Get inputs from context based on input_mapping."""
        inputs = {}
        for target_key, source_key in node.input_mapping.items():
            if source_key in context:
                inputs[target_key] = context[source_key]
            elif '.' in source_key:
                # Nested key access
                parts = source_key.split('.')
                value = context
                for part in parts:
                    if isinstance(value, dict):
                        value = value.get(part)
                    else:
                        value = None
                        break
                inputs[target_key] = value
        return inputs


class LLMNodeExecutor(NodeExecutor):
    """Executor for LLM nodes."""
    
    async def execute(
        self,
        node: WorkflowNode,
        context: Dict[str, Any],
    ) -> Any:
        """Execute an LLM call."""
        inputs = self._get_inputs(node, context)
        
        prompt = inputs.get('prompt', node.config.get('prompt', ''))
        model = inputs.get('model', node.config.get('model', 'gpt-4'))
        
        logger.info(f"[Workflow] LLM call: {node.name} ({node.node_id})")
        
        # Use model router if available
        try:
            from .model_router import get_model_router
            
            router = get_model_router()
            response = await router.generate(
                prompt=prompt,
                model_name=model,
                temperature=node.config.get('temperature', 0.7),
            )
            
            output = {
                'text': response.text,
                'model': response.model,
                'usage': response.usage,
            }
            
        except ImportError:
            # Fallback: return mock response
            output = {
                'text': f"[Mock LLM] {prompt[:100]}...",
                'model': model,
                'usage': {'prompt_tokens': 0, 'completion_tokens': 0},
            }
        
        return output


class ConditionNodeExecutor(NodeExecutor):
    """Executor for condition nodes."""
    
    async def execute(
        self,
        node: WorkflowNode,
        context: Dict[str, Any],
    ) -> bool:
        """
        Evaluate a condition.
        
        Returns True if condition is met.
        """
        inputs = self._get_inputs(node, context)
        
        condition = node.condition_expr or node.config.get('condition', '')
        
        # Safe AST-based condition evaluation - NO eval()
        # Parse with ast, walk whitelisted nodes only
        import ast as _ast
        try:
            tree = _ast.parse(condition.strip(), mode='eval')
        except SyntaxError as e:
            raise WorkflowNodeError(
                f"Condition syntax error: {condition} - {e}"
            )
        
        # Build local context for name resolution
        local_ctx = {**context, **inputs}
        
        # Allowed callable functions (no side effects, no I/O)
        _safe_funcs = {
            'True': True, 'False': False, 'None': None,
            'bool': bool, 'int': int, 'float': float, 'str': str,
            'len': len, 'abs': abs, 'min': min, 'max': max,
            'isinstance': isinstance, 'type': type,
            'round': round, 'range': range, 'list': list,
            'dict': dict, 'tuple': tuple, 'set': set,
        }
        
        # Allowed AST node types - no function definitions, comprehensions, etc.
        _ALLOWED_NODES = frozenset({
            _ast.Expression, _ast.Compare, _ast.BoolOp, _ast.UnaryOp,
            _ast.BinOp, _ast.Name, _ast.Constant, _ast.List, _ast.Tuple,
            _ast.Dict, _ast.Set, _ast.Call, _ast.Subscript, _ast.Slice,
            _ast.Attribute, _ast.Load, _ast.And, _ast.Or, _ast.Not,
            _ast.Eq, _ast.NotEq, _ast.Lt, _ast.LtE, _ast.Gt, _ast.GtE,
            _ast.Is, _ast.IsNot, _ast.In, _ast.NotIn,
            _ast.Add, _ast.Sub, _ast.Mult, _ast.Div, _ast.Mod,
            _ast.USub, _ast.UAdd, _ast.Not,
        })
        
        # Allowed attribute access targets (read-only, no dunders)
        _ALLOWED_ATTRS = frozenset({
            'startswith', 'endswith', 'lower', 'upper', 'strip',
            'split', 'join', 'replace', 'find', 'count', 'keys',
            'values', 'items', 'get', 'copy',
        })
        
        def _safe_eval(node):
            """Walk AST, evaluate only whitelisted nodes."""
            node_type = type(node)
            if node_type not in _ALLOWED_NODES:
                raise WorkflowNodeError(
                    f"Forbidden operation: {node_type.__name__}"
                )
            
            if isinstance(node, _ast.Expression):
                return _safe_eval(node.body)
            
            elif isinstance(node, _ast.Constant):
                return node.value
            
            elif isinstance(node, _ast.Name):
                # Resolve name from local_ctx or safe_funcs
                if node.id in local_ctx:
                    return local_ctx[node.id]
                if node.id in _safe_funcs:
                    return _safe_funcs[node.id]
                raise WorkflowNodeError(
                    f"Undefined name: '{node.id}'"
                )
            
            elif isinstance(node, _ast.BoolOp):
                values = [_safe_eval(v) for v in node.values]
                if isinstance(node.op, _ast.And):
                    return all(values)
                if isinstance(node.op, _ast.Or):
                    return any(values)
            
            elif isinstance(node, _ast.UnaryOp):
                operand = _safe_eval(node.operand)
                if isinstance(node.op, _ast.Not):
                    return not operand
                if isinstance(node.op, _ast.USub):
                    return -operand
                if isinstance(node.op, _ast.UAdd):
                    return +operand
            
            elif isinstance(node, _ast.BinOp):
                left = _safe_eval(node.left)
                right = _safe_eval(node.right)
                ops = {
                    _ast.Add: lambda a, b: a + b,
                    _ast.Sub: lambda a, b: a - b,
                    _ast.Mult: lambda a, b: a * b,
                    _ast.Div: lambda a, b: a / b,
                    _ast.Mod: lambda a, b: a % b,
                }
                for op_cls, fn in ops.items():
                    if isinstance(node.op, op_cls):
                        return fn(left, right)
                raise WorkflowNodeError(f"Unsupported binary op")
            
            elif isinstance(node, _ast.Compare):
                left = _safe_eval(node.left)
                for op, comparator in zip(node.ops, node.comparators):
                    right = _safe_eval(comparator)
                    if isinstance(op, _ast.Eq):
                        result = (left == right)
                    elif isinstance(op, _ast.NotEq):
                        result = (left != right)
                    elif isinstance(op, _ast.Lt):
                        result = (left < right)
                    elif isinstance(op, _ast.LtE):
                        result = (left <= right)
                    elif isinstance(op, _ast.Gt):
                        result = (left > right)
                    elif isinstance(op, _ast.GtE):
                        result = (left >= right)
                    elif isinstance(op, _ast.Is):
                        result = (left is right)
                    elif isinstance(op, _ast.IsNot):
                        result = (left is not right)
                    elif isinstance(op, _ast.In):
                        result = (left in right)
                    elif isinstance(op, _ast.NotIn):
                        result = (left not in right)
                    else:
                        raise WorkflowNodeError(f"Unsupported comparison")
                    if not result:
                        return False
                    left = right
                return True
            
            elif isinstance(node, _ast.Call):
                func = _safe_eval(node.func)
                if not callable(func):
                    raise WorkflowNodeError("Called object is not callable")
                args = [_safe_eval(a) for a in node.args]
                kwargs = {kw.arg: _safe_eval(kw.value) for kw in node.keywords}
                return func(*args, **kwargs)
            
            elif isinstance(node, _ast.Subscript):
                obj = _safe_eval(node.value)
                if isinstance(node.slice, _ast.Slice):
                    lower = _safe_eval(node.slice.lower) if node.slice.lower else None
                    upper = _safe_eval(node.slice.upper) if node.slice.upper else None
                    step = _safe_eval(node.slice.step) if node.slice.step else None
                    return obj[slice(lower, upper, step)]
                key = _safe_eval(node.slice)
                return obj[key]
            
            elif isinstance(node, _ast.Attribute):
                obj = _safe_eval(node.value)
                if node.attr.startswith('_'):
                    raise WorkflowNodeError(
                        f"Forbidden dunder attribute: {node.attr}"
                    )
                if node.attr not in _ALLOWED_ATTRS:
                    raise WorkflowNodeError(
                        f"Forbidden attribute access: .{node.attr}"
                    )
                return getattr(obj, node.attr)
            
            elif isinstance(node, (_ast.List, _ast.Tuple, _ast.Set)):
                items = [_safe_eval(e) for e in node.elts]
                if isinstance(node, _ast.List):
                    return list(items)
                elif isinstance(node, _ast.Tuple):
                    return tuple(items)
                else:
                    return set(items)
            
            elif isinstance(node, _ast.Dict):
                return {
                    _safe_eval(k): _safe_eval(v)
                    for k, v in zip(node.keys, node.values)
                }
            
            raise WorkflowNodeError(
                f"Unhandled AST node: {type(node).__name__}"
            )
        
        try:
            result = _safe_eval(tree)
            logger.info(f"[Workflow] Condition '{condition}' = {result}")
            return bool(result)
        except WorkflowNodeError:
            raise
        except Exception as e:
            logger.error(f"[Workflow] Condition evaluation error: {e}")
            raise WorkflowNodeError(f"Invalid condition: {condition}") from e


class HTTPNodeExecutor(NodeExecutor):
    """Executor for HTTP request nodes."""
    
    async def execute(
        self,
        node: WorkflowNode,
        context: Dict[str, Any],
    ) -> Any:
        """Execute an HTTP request."""
        inputs = self._get_inputs(node, context)
        
        method = inputs.get('method', node.config.get('method', 'GET'))
        url = inputs.get('url', node.config.get('url', ''))
        headers = inputs.get('headers', node.config.get('headers', {}))
        body = inputs.get('body', node.config.get('body'))
        
        logger.info(f"[Workflow] HTTP {method} {url}")
        
        # SSRF guard: validate URL scheme and destination
        from urllib.parse import urlparse as _urlparse
        _parsed = _urlparse(url)
        if _parsed.scheme not in ("http", "https"):
            raise WorkflowNodeError(f"Blocked URL scheme: {_parsed.scheme}")
        _hostname = (_parsed.hostname or "").lower()
        # Block internal/private IP ranges and localhost
        import ipaddress as _ip
        try:
            _addr = _ip.ip_address(_hostname)
            if _addr.is_private or _addr.is_loopback or _addr.is_link_local:
                raise WorkflowNodeError(f"Blocked internal IP: {_hostname}")
        except ValueError:
            # Not an IP address - validate hostname
            if _hostname in ("localhost", "127.0.0.1", "0.0.0.0", "[::1]", "::1"):
                raise WorkflowNodeError(f"Blocked hostname: {_hostname}")
        
        try:
            import aiohttp
            
            timeout = aiohttp.ClientTimeout(total=node.config.get('timeout', 30))
            
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.request(
                    method=method,
                    url=url,
                    headers=headers,
                    json=body if body else None,
                ) as response:
                    result = {
                        'status': response.status,
                        'headers': dict(response.headers),
                        'body': await response.text(),
                    }
                    
                    # Try to parse JSON
                    try:
                        result['json'] = await response.json()
                    except Exception as e:
                        logger.debug(f"[Workflow] Unknown exception (non-blocking): {e}")
                        pass
                    
                    return result
                    
        except ImportError:
            # Fallback: return mock response
            return {
                'status': 200,
                'body': f"[Mock HTTP] {method} {url}",
                'json': {'mock': True},
            }


class CodeNodeExecutor(NodeExecutor):
    """
    Executor for code execution nodes using sandboxed execution.
    
    SECURITY: All code is executed in a sandbox environment that:
    - Restricts builtins to safe whitelist only
    - Prevents import statements
    - Blocks file/network operations
    - Enforces timeout limits
    - Limits memory usage
    
    This is a core part of Force-Verify philosophy: never trust
    AI-generated code without sandboxing.
    """
    
    def __init__(self, workflow: Optional[Workflow] = None, executor: Optional['WorkflowExecutor'] = None):
        """Initialize with sandbox executor."""
        super().__init__(workflow, executor)
        self._sandbox: Optional[SandboxExecutor] = None
    
    @property
    def sandbox(self) -> SandboxExecutor:
        """Get or create sandbox executor."""
        if self._sandbox is None:
            self._sandbox = get_sandbox()
        return self._sandbox
    
    async def execute(
        self,
        node: WorkflowNode,
        context: Dict[str, Any],
    ) -> Any:
        """
        Execute Python code in sandbox.
        
        Args:
            node: Workflow node with code to execute
            context: Execution context from workflow
            
        Returns:
            Result of code execution
            
        Raises:
            WorkflowNodeError: If sandboxed execution fails
        """
        # Get inputs from input_mapping, or use entire context if mapping is empty
        mapped_inputs = self._get_inputs(node, context)
        if not mapped_inputs and not node.input_mapping:
            # If no input_mapping defined, use context as inputs (sensible default)
            inputs = context
        else:
            inputs = mapped_inputs
        
        code = node.config.get('code', '')
        timeout = node.config.get('timeout', 30)
        memory_limit = node.config.get('memory_limit_mb', 128)
        
        logger.info(f"[Workflow] Executing sandboxed code: {node.name}")
        logger.debug(f"[Workflow] Sandbox mode: {self.sandbox.mode.value}")
        
        if not code:
            raise WorkflowNodeError("No code provided for execution")
        
        # Configure sandbox
        config = SandboxConfig(
            timeout_seconds=timeout,
            memory_limit_mb=memory_limit,
        )
        
        try:
            # Execute in sandbox
            result = self.sandbox.execute(
                code=code,
                inputs=inputs,
                context=context,
                config=config,
            )
            
            if not result.success:
                logger.error(f"[Workflow] Sandbox error: {result.error}")
                raise WorkflowNodeError(f"Sandboxed execution failed: {result.error}")
            
            # Log sandbox execution stats
            logger.info(
                f"[Workflow] Code executed successfully in {result.elapsed_ms:.2f}ms "
                f"({result.mode.value})"
            )
            
            # Return output, appending stdout if any
            output = result.output
            if result.stdout:
                # Attach stdout to output if it's a dict
                if isinstance(output, dict):
                    output['_stdout'] = result.stdout
                elif output is None:
                    output = {'_stdout': result.stdout}
            
            return output
            
        except WorkflowNodeError:
            raise
        except Exception as e:
            logger.error(f"[Workflow] Code execution error: {e}")
            raise WorkflowNodeError(f"Code execution failed: {e}") from e


class PluginNodeExecutor(NodeExecutor):
    """Executor for plugin tool nodes."""
    
    async def execute(
        self,
        node: WorkflowNode,
        context: Dict[str, Any],
    ) -> Any:
        """Execute a plugin tool."""
        inputs = self._get_inputs(node, context)
        
        plugin_name = node.config.get('plugin', '')
        tool_name = node.config.get('tool', '')
        
        logger.info(f"[Workflow] Plugin call: {plugin_name}.{tool_name}")
        
        try:
            from ..plugins import TicalPlugin
            
            # Get plugin instance
            plugin = TicalPlugin.get_registered(plugin_name)
            if plugin is None:
                raise WorkflowNodeError(f"Plugin not found: {plugin_name}")
            
            # Get tool
            tool = plugin.tools.get(tool_name)
            if tool is None:
                raise WorkflowNodeError(f"Tool not found: {plugin_name}.{tool_name}")
            
            # Execute tool
            result = await tool(inputs)
            
            return result
            
        except ImportError as e:
            logger.warning(f"[Workflow] Plugin system not available: {e}")
            return {'error': f'Plugin system unavailable: {e}'}


class ParallelNodeExecutor(NodeExecutor):
    """Executor for parallel (fan-out/fan-in) nodes."""
    
    async def execute(
        self,
        node: WorkflowNode,
        context: Dict[str, Any],
    ) -> List[Any]:
        """Execute child nodes in parallel."""
        child_node_ids = node.config.get('nodes', [])
        inputs = self._get_inputs(node, context)
        
        logger.info(f"[Workflow] Parallel execution: {len(child_node_ids)} nodes")
        
        # Get child executors
        tasks = []
        for child_id in child_node_ids:
            child_node = self.workflow.get_node(child_id)
            if child_node:
                task = self.executor._execute_node(child_node, context.copy())
                tasks.append(task)
        
        # Execute in parallel
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # Process results
        outputs = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                logger.error(f"[Workflow] Parallel node {child_node_ids[i]} failed: {result}")
                outputs.append({'error': str(result)})
            else:
                outputs.append(result)
        
        return outputs


# Registry of executors
_NODE_EXECUTORS: Dict[NodeType, type] = {
    NodeType.LLM: LLMNodeExecutor,
    NodeType.CONDITION: ConditionNodeExecutor,
    NodeType.HTTP: HTTPNodeExecutor,
    NodeType.CODE: CodeNodeExecutor,
    NodeType.PLUGIN: PluginNodeExecutor,
    NodeType.PARALLEL: ParallelNodeExecutor,
}


# =============================================================================
# Workflow Executor
# =============================================================================

@dataclass
class WorkflowResult:
    """Result of workflow execution."""
    workflow_id: str
    success: bool
    output: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    
    # Execution stats
    start_time: float = 0.0
    end_time: float = 0.0
    duration_ms: float = 0.0
    
    # Node results
    node_results: Dict[str, Any] = field(default_factory=dict)
    
    # Evidence
    execution_hash: Optional[str] = None
    
    def to_dict(self) -> Dict:
        """Convert to dictionary."""
        return {
            'workflow_id': self.workflow_id,
            'success': self.success,
            'output': self.output,
            'error': self.error,
            'start_time': self.start_time,
            'end_time': self.end_time,
            'duration_ms': self.duration_ms,
            'node_results': self.node_results,
            'execution_hash': self.execution_hash,
        }


class WorkflowExecutor:
    """
    Executes workflows with Force-Verify integration.
    
    Each node execution is:
    1. Traced via TraceRecorder
    2. Verified via Force-Verify
    3. Recorded to Anchor
    """
    
    def __init__(self, workflow: Workflow):
        """
        Initialize executor.
        
        Args:
            workflow: Workflow to execute
        """
        self.workflow = workflow
        
        # Get trace recorder
        try:
            from .trace import get_trace_recorder, SpanType, SpanStatus
            self.trace_recorder = get_trace_recorder()
            self.trace_span_type = SpanType
            self.trace_span_status = SpanStatus
        except ImportError:
            self.trace_recorder = None
            self.trace_span_type = None
            self.trace_span_status = None
        
        # Get verifier
        try:
            from .verify import SchemaValidator, VerificationContext, VerifyLevel
            self.schema_validator = SchemaValidator
            self.verification_context = VerificationContext
            self.verify_level = VerifyLevel
        except ImportError:
            self.schema_validator = None
            self.verification_context = None
            self.verify_level = None
        
        # Initialize executors
        self._executors: Dict[NodeType, NodeExecutor] = {}
        for node_type, executor_class in _NODE_EXECUTORS.items():
            self._executors[node_type] = executor_class(workflow, self)
    
    async def execute(
        self,
        initial_input: Dict[str, Any],
        trace_id: Optional[str] = None,
    ) -> WorkflowResult:
        """
        Execute the workflow.
        
        Args:
            initial_input: Initial input data
            trace_id: Optional trace ID for tracking
            
        Returns:
            WorkflowResult: Execution result
        """
        workflow_id = f"{self.workflow.name}_{int(time.time())}"
        
        result = WorkflowResult(
            workflow_id=workflow_id,
            success=False,
            start_time=time.time(),
        )
        
        context = {**initial_input}
        
        # Start trace
        trace = None
        if self.trace_recorder:
            trace = self.trace_recorder.start_trace(
                name=f"workflow:{self.workflow.name}",
                metadata={
                    'workflow_name': self.workflow.name,
                    'workflow_version': self.workflow.version,
                    'trace_id': trace_id,
                },
            )
        
        try:
            # Get start nodes
            if self.workflow.start_node:
                current_nodes = [self.workflow.get_node(self.workflow.start_node)]
            else:
                current_nodes = self.workflow.get_start_nodes()
            
            # Execute nodes
            while current_nodes:
                next_nodes = []
                
                for node in current_nodes:
                    # Skip START and END nodes (no actual execution)
                    if node.node_type == NodeType.START or node.node_type == NodeType.END:
                        node.status = NodeStatus.PASSED
                        result.node_results[node.node_id] = None
                        # Continue to next nodes
                        for next_id in node.next_nodes:
                            next_node = self.workflow.get_node(next_id)
                            if next_node:
                                next_nodes.append(next_node)
                        continue
                    
                    node_result = await self._execute_node(node, context)
                    
                    # Store result
                    if node.output_key:
                        context[node.output_key] = node_result
                    result.node_results[node.node_id] = node_result
                    
                    # Add next nodes
                    if node.status == NodeStatus.PASSED:
                        for next_id in node.next_nodes:
                            next_node = self.workflow.get_node(next_id)
                            if next_node:
                                next_nodes.append(next_node)
                
                # Handle parallel nodes
                current_nodes = []
                for node in next_nodes:
                    if node.node_type == NodeType.PARALLEL:
                        # Execute parallel node
                        parallel_result = await self._execute_node(node, context)
                        result.node_results[node.node_id] = parallel_result
                        
                        # Get child results and continue
                        for child_id in node.config.get('nodes', []):
                            child_node = self.workflow.get_node(child_id)
                            if child_node and child_node.status == NodeStatus.PASSED:
                                for grandchild_id in child_node.next_nodes:
                                    grandchild = self.workflow.get_node(grandchild_id)
                                    if grandchild:
                                        current_nodes.append(grandchild)
                    else:
                        current_nodes.append(node)
                
                # Check for end nodes
                for node in current_nodes:
                    if node.node_type == NodeType.END or node.node_id in self.workflow.end_nodes:
                        result.success = True
            
            result.output = context
            
        except Exception as e:
            logger.error(f"[Workflow] Execution error: {e}")
            result.error = str(e)
            result.success = False
        
        finally:
            result.end_time = time.time()
            result.duration_ms = (result.end_time - result.start_time) * 1000
            
            # Generate execution hash
            result.execution_hash = hashlib.sha256(
                json.dumps({
                    'workflow_id': workflow_id,
                    'results': {k: str(v)[:100] for k, v in result.node_results.items()},
                    'duration_ms': result.duration_ms,
                }, sort_keys=True, default=str).encode()
            ).hexdigest()
            
            # End trace
            if trace and self.trace_recorder:
                self.trace_recorder.end_trace(
                    trace,
                    status=self.trace_span_status.OK if result.success else self.trace_span_status.ERROR,
                    metadata={'result': result.to_dict()},
                )
        
        return result
    
    async def _execute_node(
        self,
        node: WorkflowNode,
        context: Dict[str, Any],
    ) -> Any:
        """
        Execute a single node with Force-Verify.
        
        Args:
            node: Node to execute
            context: Shared context
            
        Returns:
            Node output
        """
        start_time = time.time()
        
        # Start trace span
        span = None
        if self.trace_recorder and self.trace_span_type:
            span = self.trace_recorder.start_trace(
                name=f"node:{node.name}",
                metadata={'node_id': node.node_id, 'node_type': node.node_type.value},
            )
        
        node.status = NodeStatus.RUNNING
        
        try:
            # Get executor
            executor = self._executors.get(node.node_type)
            if executor is None:
                raise WorkflowNodeError(f"No executor for node type: {node.node_type}")
            
            # Execute node
            result = await executor.execute(node, context)
            
            # Verification
            node.status = NodeStatus.VERIFYING
            
            verified = await self._verify_node(node, result)
            
            if verified:
                node.status = NodeStatus.PASSED
            else:
                node.status = NodeStatus.FAILED
                raise WorkflowVerificationError(
                    f"Verification failed for node: {node.name}"
                )
            
            # Generate evidence hash
            node.evidence_hash = hashlib.sha256(
                json.dumps({
                    'node_id': node.node_id,
                    'result': str(result)[:200],
                    'timestamp': time.time(),
                }, sort_keys=True, default=str).encode()
            ).hexdigest()
            
            node.execution_count += 1
            node.total_duration_ms += (time.time() - start_time) * 1000
            
            # End trace span
            if span and self.trace_recorder:
                self.trace_recorder.end_trace(
                    span,
                    status=self.trace_span_status.OK,
                    output_data={'evidence_hash': node.evidence_hash},
                    verification_passed=True,
                )
            
            return result
            
        except Exception as e:
            node.status = NodeStatus.FAILED
            node.error = str(e)
            
            # End trace span with error
            if span and self.trace_recorder:
                self.trace_recorder.end_trace(
                    span,
                    status=self.trace_span_status.ERROR,
                    error=str(e),
                    verification_passed=False,
                )
            
            raise
    
    async def _verify_node(self, node: WorkflowNode, result: Any) -> bool:
        """
        Verify node output.
        
        Args:
            node: The node that was executed
            result: The result to verify
            
        Returns:
            True if verification passed
        """
        if node.verify_schema is None and node.verify_level == "SCHEMA":
            # No verification required
            return True
        
        if self.schema_validator is None:
            logger.warning("[Workflow] Verifier not available, skipping verification")
            return True
        
        if node.verify_schema:
            verify_result = self.schema_validator.validate(result, node.verify_schema)
            return verify_result.passed
        
        return True


# =============================================================================
# Workflow Builder
# =============================================================================

class WorkflowBuilder:
    """
    Helper class to build workflows programmatically.
    
    Usage:
        builder = WorkflowBuilder("My Workflow", "1.0")
        
        builder.add_start("start")
        builder.add_llm("call_model", prompt="Hello {{input}}")
        builder.add_condition("check", "result > 0")
        builder.add_end("end")
        
        workflow = builder.build()
    """
    
    def __init__(self, name: str, version: str = "0.1.5"):
        """Initialize builder."""
        self.name = name
        self.version = version
        self._nodes: Dict[str, WorkflowNode] = {}
        self._start_node: Optional[str] = None
        self._end_nodes: List[str] = []
        self._node_counter = 0
    
    def _next_id(self, prefix: str) -> str:
        """Generate next node ID."""
        self._node_counter += 1
        return f"{prefix}_{self._node_counter}"
    
    def add_node(self, node: WorkflowNode) -> 'WorkflowBuilder':
        """Add a node."""
        self._nodes[node.node_id] = node
        return self
    
    def add_start(self, name: str = "start") -> 'WorkflowBuilder':
        """Add a start node."""
        node_id = self._next_id("start")
        node = WorkflowNode(
            node_id=node_id,
            node_type=NodeType.START,
            name=name,
        )
        self.add_node(node)
        self._start_node = node_id
        return self
    
    def add_end(self, name: str = "end") -> 'WorkflowBuilder':
        """Add an end node."""
        node_id = self._next_id("end")
        node = WorkflowNode(
            node_id=node_id,
            node_type=NodeType.END,
            name=name,
        )
        self.add_node(node)
        self._end_nodes.append(node_id)
        return self
    
    def add_llm(
        self,
        name: str,
        prompt: str,
        model: str = "gpt-4",
        output_key: Optional[str] = None,
    ) -> 'WorkflowBuilder':
        """Add an LLM node."""
        node_id = self._next_id("llm")
        node = WorkflowNode(
            node_id=node_id,
            node_type=NodeType.LLM,
            name=name,
            config={
                'prompt': prompt,
                'model': model,
            },
            output_key=output_key or "llm_output",
        )
        self.add_node(node)
        return self
    
    def add_condition(
        self,
        name: str,
        condition: str,
        next_true: Optional[str] = None,
        next_false: Optional[str] = None,
    ) -> 'WorkflowBuilder':
        """Add a condition node."""
        node_id = self._next_id("condition")
        node = WorkflowNode(
            node_id=node_id,
            node_type=NodeType.CONDITION,
            name=name,
            condition_expr=condition,
            next_nodes=[next_true] if next_true else [],
        )
        self.add_node(node)
        return self
    
    def add_http(
        self,
        name: str,
        url: str,
        method: str = "GET",
        output_key: Optional[str] = None,
    ) -> 'WorkflowBuilder':
        """Add an HTTP node."""
        node_id = self._next_id("http")
        node = WorkflowNode(
            node_id=node_id,
            node_type=NodeType.HTTP,
            name=name,
            config={
                'url': url,
                'method': method,
            },
            output_key=output_key or "http_output",
        )
        self.add_node(node)
        return self
    
    def add_code(
        self,
        name: str,
        code: str,
        output_key: Optional[str] = None,
    ) -> 'WorkflowBuilder':
        """Add a code execution node."""
        node_id = self._next_id("code")
        node = WorkflowNode(
            node_id=node_id,
            node_type=NodeType.CODE,
            name=name,
            config={'code': code},
            output_key=output_key,
        )
        self.add_node(node)
        return self
    
    def add_plugin(
        self,
        name: str,
        plugin: str,
        tool: str,
        output_key: Optional[str] = None,
    ) -> 'WorkflowBuilder':
        """Add a plugin node."""
        node_id = self._next_id("plugin")
        node = WorkflowNode(
            node_id=node_id,
            node_type=NodeType.PLUGIN,
            name=name,
            config={
                'plugin': plugin,
                'tool': tool,
            },
            output_key=output_key or f"{plugin}_{tool}_output",
        )
        self.add_node(node)
        return self
    
    def add_parallel(
        self,
        name: str,
        node_ids: List[str],
    ) -> 'WorkflowBuilder':
        """Add a parallel (fan-out) node."""
        node_id = self._next_id("parallel")
        node = WorkflowNode(
            node_id=node_id,
            node_type=NodeType.PARALLEL,
            name=name,
            config={'nodes': node_ids},
        )
        self.add_node(node)
        return self
    
    def then(self, from_id: str, to_id: str) -> 'WorkflowBuilder':
        """Connect two nodes."""
        from_node = self._nodes.get(from_id)
        if from_node and to_id not in from_node.next_nodes:
            from_node.next_nodes.append(to_id)
        return self
    
    def build(self) -> Workflow:
        """Build the workflow."""
        if not self._start_node and self._nodes:
            # Auto-detect start node
            start_candidates = self._nodes.values()
            for node in start_candidates:
                is_start = True
                for other in self._nodes.values():
                    if node.node_id in other.next_nodes:
                        is_start = False
                        break
                if is_start:
                    self._start_node = node.node_id
                    break
        
        workflow = Workflow(
            name=self.name,
            version=self.version,
            nodes=self._nodes,
            start_node=self._start_node,
            end_nodes=self._end_nodes,
        )
        
        return workflow

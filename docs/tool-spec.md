# tical-code Tool Interface Specification (v0.3)

## Overview

This document defines tical-code's tool interface standard. All built-in and custom tools must conform to this specification.

## ToolDefinition Data Structure

```python
@dataclass
class ToolDefinition:
    name: str                      # Unique identifier
    description: str               # AI-readable description
    params: Dict[str, Any]         # JSON Schema format parameter definition
    handler: Callable[..., Any]    # Execution function
    verify_level: VerifyLevel      # Verification level (default SCHEMA)
    timeout: int                   # Timeout in seconds (default 30)
    edition: str                   # Applicable edition: "lite" / "full" / "both"
    requires_confirmation: bool    # Whether human confirmation is needed
    allowed_roles: List[str]       # Callable roles ["all"] or specific role list
```

## Parameter Validation Rules (JSON Schema)

### Basic Structure

```json
{
  "type": "object",
  "properties": {
    "param_name": {
      "type": "string|number|integer|boolean|array|object",
      "description": "Parameter description",
      "minimum": 0,
      "maximum": 100,
      "default": "default value",
      "enum": ["option1", "option2"]
    }
  },
  "required": ["required_param1", "required_param2"]
}
```

### Type Check Rules

| JSON Schema Type | Python Type |
|------------------|-------------|
| `string` | `str` |
| `number` | `int`, `float` |
| `integer` | `int` |
| `boolean` | `bool` |
| `array` | `list` |
| `object` | `dict` |
| `null` | `None` |

### Validation Flow

1. Check required parameters exist
2. Check parameter types match
3. Check numeric ranges (minimum/maximum)
4. Check enum values are in the allowed list

## Handler Signature and Return Format

### Handler Signature

```python
async def tool_handler(params: Dict[str, Any], context: Dict[str, Any]) -> ToolResult:
    """
    Args:
        params: Validated parameter dictionary
        context: Execution context {
            "session_id": str,
            "user_id": str,
            "role": str,
            "config": WorkerConfig,
            ...
        }
    
    Returns:
        ToolResult object
    """
```

### Return Format (ToolResult)

```python
@dataclass
class ToolResult:
    success: bool           # Whether successful
    data: Any = None       # Return data
    error: str = None       # Error message (on failure)
    verified: bool = False  # Whether Force-Verify was applied
    elapsed_ms: float = 0  # Execution time in ms
```

## Force-Verify Integration Requirements

### Verification Levels

| Level | Name | Description |
|-------|------|-------------|
| 0 | NONE | No verification |
| 1 | BASIC | Check return value is not None |
| 2 | SCHEMA | Validate return data structure (default) |
| 3 | DUAL | AI + Schema dual verification |
| 4 | HUMAN | Requires human confirmation |
| 5 | IDENTITY | Identity verification + above levels |

### Decorator Usage

```python
from tical_code.core.verify import force_verify, VerifyLevel

@force_verify(VerifyLevel.SCHEMA)
async def read_file(params: Dict, context: Dict) -> ToolResult:
    # Tool implementation
    pass
```

### Built-in Tool Default Verification Levels

| Tool | Default VerifyLevel |
|------|---------------------|
| read_file | SCHEMA |
| write_file | DUAL |
| shell_exec | IDENTITY |
| http_get | BASIC |
| http_post | BASIC |
| patch_file | IDENTITY |
| extract_text | BASIC |

## Error Code Standards

### Error Code Definitions

| Code | Name | Description |
|------|------|-------------|
| 1001 | INVALID_PARAMS | Parameter validation failed |
| 1002 | MISSING_REQUIRED | Missing required parameter |
| 1003 | TYPE_MISMATCH | Type mismatch |
| 1004 | RANGE_EXCEEDED | Value exceeds allowed range |
| 2001 | PERMISSION_DENIED | Insufficient permissions |
| 2002 | ROLE_NOT_ALLOWED | Role not allowed to call |
| 3001 | EXECUTION_TIMEOUT | Execution timeout |
| 3002 | EXECUTION_ERROR | Execution error |
| 4001 | VERIFICATION_FAILED | Verification failed |
| 4002 | CONFIRMATION_REJECTED | User rejected confirmation |

### Error Response Format

```json
{
  "success": false,
  "error": {
    "code": 1001,
    "name": "INVALID_PARAMS",
    "message": "Parameter 'timeout' value 999 exceeds allowed range (1-60)",
    "details": {
      "param": "timeout",
      "value": 999,
      "min": 1,
      "max": 60
    }
  }
}
```

## How to Register Custom Tools

### Method 1: Direct Registration

```python
from tical_code.core.tool_registry import ToolRegistry, ToolDefinition, VerifyLevel

# Define tool
my_tool = ToolDefinition(
    name="my_custom_tool",
    description="Perform a custom operation",
    params={
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["start", "stop"]},
            "target": {"type": "string"}
        },
        "required": ["action"]
    },
    handler=my_handler,
    verify_level=VerifyLevel.SCHEMA,
    edition="both"
)

# Register
registry = ToolRegistry()
await registry.register(my_tool)
```

### Method 2: Via Decorator

```python
from tical_code.core.tool_registry import register_tool

@register_tool(
    name="quick_tool",
    description="Quick tool",
    params_schema={"type": "object", "properties": {"x": {"type": "integer"}}},
    verify_level=VerifyLevel.BASIC,
    allowed_roles=["admin", "operator"]
)
async def quick_handler(params: Dict, context: Dict) -> ToolResult:
    return ToolResult(success=True, data={"result": params["x"] * 2})
```

### Method 3: Via Configuration File

```json
{
  "custom_tools": [
    {
      "name": "my_tool",
      "module": "my_package.tools",
      "function": "my_handler",
      "description": "My custom tool",
      "params_schema": {...},
      "verify_level": "SCHEMA"
    }
  ]
}
```

## Tool-Level Permission Control

### allowed_roles Field

```python
@dataclass
class ToolDefinition:
    # ... other fields
    allowed_roles: List[str] = field(default_factory=lambda: ["all"])
```

### Role Definitions

| Role | Description |
|------|-------------|
| `all` | All users/roles can call |
| `admin` | Admin only |
| `operator` | Operator only |
| `user` | Regular user |
| `guest` | Guest (restricted) |
| `system` | Internal system calls |

### Permission Check Flow

```
1. Get caller role (from context["role"])
2. Check tool.allowed_roles
3. If contains "all" → allow
4. If caller role is in allowed_roles → allow
5. Otherwise → return PERMISSION_DENIED error
```

### Examples

```python
# Admin only
admin_tool = ToolDefinition(
    name="system_admin",
    allowed_roles=["admin"]
)

# Everyone can use
public_tool = ToolDefinition(
    name="public_info",
    allowed_roles=["all"]  # default
)

# Operator and admin
restricted_tool = ToolDefinition(
    name="sensitive_ops",
    allowed_roles=["admin", "operator"]
)
```

## Built-in Tool List

| Tool | Description | VerifyLevel | Roles |
|------|-------------|-------------|-------|
| read_file | Read file content | SCHEMA | all |
| write_file | Write to file | DUAL | all |
| list_dir | List directory | BASIC | all |
| shell_exec | Execute shell commands | IDENTITY | admin, operator |
| http_get | HTTP GET request | BASIC | all |
| http_post | HTTP POST request | BASIC | all |
| patch_file | Patch-edit files | IDENTITY | admin |
| extract_text | Extract text from web pages | BASIC | all |
| search_files | Search for files | BASIC | all |
| session_create | Create session | SCHEMA | all |
| session_get | Get session | SCHEMA | all |
| memory_read | Read memory | BASIC | all |
| memory_write | Write memory | SCHEMA | operator, admin |

## Version Compatibility

- **v0.3**: Current version, full feature implementation
- **v0.2**: Basic version, no permission control
- **v0.1**: Early version, no verify_level

Backward compatibility:
- `allowed_roles` defaults to `["all"]` (compatible with older versions)
- `verify_level` defaults to `SCHEMA` (compatible with older versions)

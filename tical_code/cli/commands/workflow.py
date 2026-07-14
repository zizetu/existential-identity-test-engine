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
Workflow CLI Commands (eite-agent v0.1.5)
=======================================

Workflow management commands:
- workflow run: Execute a workflow
- workflow list: List available workflows
- workflow show: Show workflow definition
"""

import asyncio
import json
import sys
import click
import logging
from typing import Optional

logger = logging.getLogger(__name__)


@click.group('workflow')
def workflow_group():
    """
    Manage and execute workflows.
    """
    pass


@workflow_group.command('run')
@click.argument('workflow_file', required=False)
@click.option('--name', help='Workflow name (for inline definition)')
@click.option('--input', '-i', 'input_data', multiple=True,
              help='Input data as key=value pairs')
@click.option('--json-input', help='Input data as JSON string')
@click.option('--trace/--no-trace', default=True, help='Enable trace recording')
def workflow_run(workflow_file: Optional[str], name: Optional[str], 
                 input_data: tuple, json_input: Optional[str], trace: bool):
    """
    Execute a workflow.
    
    Examples:
        tical workflow run workflow.json
        tical workflow run --name "My Workflow" --input key=value
    """
    from tical_code.core.workflow import WorkflowExecutor, Workflow
    
    # Load or create workflow
    if workflow_file:
        try:
            with open(workflow_file, 'r') as f:
                if workflow_file.endswith('.json'):
                    workflow_data = json.load(f)
                else:
                    workflow_data = json.loads(f.read())
            workflow = Workflow.from_dict(workflow_data)
        except Exception as e:
            click.echo(f"Error loading workflow: {e}", err=True)
            sys.exit(1)
    elif name:
        # Create a simple default workflow
        from tical_code.core.workflow import WorkflowBuilder, NodeType
        builder = WorkflowBuilder(name)
        builder.add_start("start")
        builder.add_code("process", "return inputs", output_key="result")
        builder.add_end("end")
        workflow = builder.build()
    else:
        click.echo("Error: Either --workflow-file or --name required", err=True)
        sys.exit(1)
    
    # Parse input data
    inputs = {}
    if json_input:
        try:
            inputs = json.loads(json_input)
        except Exception as e:
            click.echo(f"Error parsing JSON input: {e}", err=True)
            sys.exit(1)
    
    for item in input_data:
        if '=' in item:
            key, value = item.split('=', 1)
            inputs[key] = value
    
    click.echo(f"Executing workflow: {workflow.name}")
    click.echo(f"Input: {json.dumps(inputs, indent=2)}")
    click.echo()
    
    # Execute
    executor = WorkflowExecutor(workflow)
    
    async def run():
        result = await executor.execute(inputs, trace_enabled=trace)
        return result
    
    result = asyncio.run(run())
    
    # Output results
    click.echo("=" * 50)
    click.echo(f"Workflow Result: {'SUCCESS' if result.success else 'FAILED'}")
    click.echo(f"Duration: {result.duration_ms:.1f}ms")
    click.echo(f"Execution Hash: {result.execution_hash}")
    click.echo()
    
    if result.output:
        click.echo("Output:")
        click.echo(json.dumps(result.output, indent=2, default=str))
    
    if result.error:
        click.echo(f"\nError: {result.error}", err=True)
    
    # Node results
    click.echo("\nNode Results:")
    for node_id, node_result in result.node_results.items():
        click.echo(f"  {node_id}: {str(node_result)[:60]}...")
    
    sys.exit(0 if result.success else 1)


@workflow_group.command('list')
@click.option('--format', type=click.Choice(['table', 'json']), default='table')
def workflow_list(format: str):
    """
    List available workflows.
    
    Searches for workflow definitions in standard locations.
    """
    import os
    from pathlib import Path
    
    workflows = []
    
    # Search paths
    search_paths = [
        Path.cwd() / 'workflows',
        Path.home() / '.tical' / 'workflows',
        Path(__file__).parent.parent.parent.parent / 'workflows',
    ]
    
    for search_path in search_paths:
        if not search_path.exists():
            continue
        
        for wf_file in search_path.glob('*.json'):
            try:
                with open(wf_file) as f:
                    data = json.load(f)
                    workflows.append({
                        'name': data.get('name', wf_file.stem),
                        'version': data.get('version', '0.1.5'),
                        'path': str(wf_file),
                        'node_count': len(data.get('nodes', {})),
                    })
            except Exception as e:
                logger.debug(f"[workflow.py] Operation failed (non-blocking): {e}")
    
    if format == 'json':
        click.echo(json.dumps(workflows, indent=2))
    else:
        if not workflows:
            click.echo("No workflows found.")
            return
        
        click.echo(f"Found {len(workflows)} workflow(s):\n")
        
        # Simple table
        click.echo(f"{'Name':<30} {'Version':<10} {'Nodes':<10} Path")
        click.echo("-" * 80)
        
        for wf in workflows:
            click.echo(f"{wf['name']:<30} {wf['version']:<10} {wf['node_count']:<10} {wf['path']}")


@workflow_group.command('show')
@click.argument('workflow_file')
@click.option('--format', type=click.Choice(['yaml', 'json']), default='yaml')
def workflow_show(workflow_file: str, format: str):
    """
    Show workflow definition.
    
    Examples:
        tical workflow show workflow.json
    """
    import yaml
    
    try:
        with open(workflow_file, 'r') as f:
            if workflow_file.endswith('.json'):
                data = json.load(f)
            else:
                data = json.load(f)
        
        # Simplify for display
        if format == 'yaml':
            click.echo(yaml.dump(data, default_flow_style=False, sort_keys=False))
        else:
            click.echo(json.dumps(data, indent=2))
            
    except Exception as e:
        click.echo(f"Error reading workflow: {e}", err=True)
        sys.exit(1)

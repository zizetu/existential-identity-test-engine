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
# Original repository: https://github.com/zizetu/eite-agent
#

"""
Evaluation CLI Commands (eite-agent v0.1.2)
========================================

Honesty evaluation commands:
- eval run: Run an evaluation suite
- eval list: List evaluation reports
- eval report: Show evaluation report
"""

import asyncio
import json
import sys
import click
from typing import Optional


@click.group('eval')
def eval_group():
    """
    Run honesty evaluations.
    """
    pass


@eval_group.command('run')
@click.argument('suite_file', required=False)
@click.option('--name', help='Evaluation suite name')
@click.option('--prompt', '-p', multiple=True, help='Test prompts')
@click.option('--parallel/--sequential', default=False, help='Run in parallel')
@click.option('--trace/--no-trace', default=True, help='Enable trace recording')
@click.option('--output', '-o', help='Output file for report')
def eval_run(suite_file: Optional[str], name: Optional[str],
             prompt: tuple, parallel: bool, trace: bool, output: Optional[str]):
    """
    Run an honesty evaluation.
    
    Examples:
        tical eval run --name "My Eval" --prompt "What is 2+2?" --prompt "Who is the president?"
        tical eval run suite.json
    """
    from tical_code.core.eval import EvalSuite, EvalCase, create_eval_case
    import uuid
    
    # Create or load suite
    if suite_file:
        try:
            with open(suite_file, 'r') as f:
                data = json.load(f)
            
            suite = EvalSuite(name=data.get('name', 'Loaded Suite'))
            
            # Load cases
            for case_data in data.get('cases', []):
                suite.add_case(EvalCase(
                    case_id=case_data.get('case_id', str(uuid.uuid4())[:16]),
                    name=case_data['name'],
                    prompt=case_data['prompt'],
                    tags=case_data.get('tags', []),
                ))
        except Exception as e:
            click.echo(f"Error loading suite: {e}", err=True)
            sys.exit(1)
    else:
        # Create new suite from prompts
        suite_name = name or "Quick Eval"
        suite = EvalSuite(name=suite_name)
        
        if not prompt:
            click.echo("Error: No prompts provided", err=True)
            sys.exit(1)
        
        for i, p in enumerate(prompt):
            suite.add_case(create_eval_case(
                name=f"test_{i+1}",
                prompt=p,
            ))
    
    # Configure
    suite.run_parallel = parallel
    
    # Mock agent function for testing
    async def mock_agent(prompt: str):
        await asyncio.sleep(0.1)  # Simulate processing
        return f"[Mock Response to: {prompt[:50]}...]"
    
    suite.set_agent(mock_agent)
    
    click.echo(f"Running evaluation: {suite.name}")
    click.echo(f"Cases: {len(suite.cases)}")
    click.echo(f"Mode: {'Parallel' if parallel else 'Sequential'}")
    click.echo()
    
    # Run
    async def run():
        report = await suite.run(trace_enabled=trace)
        return report
    
    report = asyncio.run(run())
    
    # Output summary
    click.echo("=" * 50)
    click.echo("Evaluation Report")
    click.echo("=" * 50)
    click.echo(f"Report ID: {report.report_id}")
    click.echo(f"Suite: {report.suite_name}")
    click.echo(f"Duration: {report.total_duration_ms:.1f}ms")
    click.echo()
    
    click.echo("Summary:")
    click.echo(f"  Total Cases: {report.total_cases}")
    click.echo(f"  Passed: {report.passed_cases}")
    click.echo(f"  Failed: {report.failed_cases}")
    click.echo(f"  Pass Rate: {report.passed_cases / report.total_cases * 100:.1f}%" if report.total_cases > 0 else "  Pass Rate: N/A")
    click.echo()
    
    click.echo("Honesty Metrics (avg scores):")
    click.echo(f"  Consistency: {report.avg_consistency:.2f}")
    click.echo(f"  Verifiability: {report.avg_verifiability:.2f}")
    click.echo(f"  Evidence: {report.avg_evidence:.2f}")
    click.echo(f"  Hallucination: {report.avg_hallucination:.2f}")
    click.echo(f"  Overall Honesty: {report.avg_honesty:.2f}")
    click.echo()
    
    # Individual results
    click.echo("Case Results:")
    for result in report.results:
        status_icon = "[v]" if result.status.value == "passed" else "[x]"
        click.echo(f"  {status_icon} {result.case_name}: honesty={result.honesty_score:.2f}")
    
    # Save to file if requested
    if output:
        try:
            with open(output, 'w') as f:
                json.dump(report.to_dict(), f, indent=2)
            click.echo(f"\nReport saved to: {output}")
        except Exception as e:
            click.echo(f"Error saving report: {e}", err=True)
    
    sys.exit(0 if report.failed_cases == 0 else 1)


@eval_group.command('list')
@click.option('--format', type=click.Choice(['table', 'json']), default='table')
@click.option('--limit', '-n', default=10, help='Number of reports to show')
def eval_list(format: str, limit: int):
    """
    List evaluation reports.
    
    Shows recent evaluation results stored in anchor.
    """
    from tical_code.core.anchor import get_anchor_manager
    
    try:
        anchor_mgr = get_anchor_manager()
        
        # Get all eval reports from anchor
        reports = []
        
        if anchor_mgr:
            for key, anchor in anchor_mgr._anchors.items():
                if key.startswith('eval_report:'):
                    reports.append(anchor.value)
        
        reports = reports[:limit]
        
        if format == 'json':
            click.echo(json.dumps(reports, indent=2))
        else:
            if not reports:
                click.echo("No evaluation reports found.")
                return
            
            click.echo(f"Recent Evaluation Reports:\n")
            click.echo(f"{'Report ID':<20} {'Suite':<20} {'Cases':<8} {'Passed':<8} {'Honesty':<10}")
            click.echo("-" * 70)
            
            for r in reports:
                click.echo(f"{r.get('report_id', 'N/A'):<20} {r.get('suite_name', 'N/A'):<20} "
                          f"{r.get('total_cases', 0):<8} {r.get('passed_cases', 0):<8} "
                          f"{r.get('avg_honesty', 0):.2f}")
                
    except Exception as e:
        click.echo(f"Error listing reports: {e}", err=True)
        sys.exit(1)


@eval_group.command('report')
@click.argument('report_id')
@click.option('--format', type=click.Choice(['yaml', 'json']), default='yaml')
def eval_report(report_id: str, format: str):
    """
    Show detailed evaluation report.
    
    Examples:
        tical eval report eval_abc123
    """
    import yaml
    from tical_code.core.anchor import get_anchor_manager
    
    try:
        anchor_mgr = get_anchor_manager()
        
        if anchor_mgr is None:
            click.echo("Anchor manager not available", err=True)
            sys.exit(1)
        
        # Find report in anchor
        report = None
        
        for key, anchor in anchor_mgr._anchors.items():
            if key == f'eval_report:{report_id}' or key == f'eval_report:eval_{report_id}':
                report = anchor.value
                break
        
        if report is None:
            click.echo(f"Report not found: {report_id}", err=True)
            sys.exit(1)
        
        if format == 'yaml':
            click.echo(yaml.dump(report, default_flow_style=False, sort_keys=False))
        else:
            click.echo(json.dumps(report, indent=2))
            
    except Exception as e:
        click.echo(f"Error loading report: {e}", err=True)
        sys.exit(1)

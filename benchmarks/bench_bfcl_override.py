import logging
logger = logging.getLogger(__name__)

class BFCLOverrideRunner:
    """Override runner for BFCL benchmark with stats merging hook."""
    
    def run(self, agent_fn, split="test", max_tasks=0):
        """Override: add merge_agent_stats hook"""
        results = super().run(agent_fn, split, max_tasks)
        # if BFCLBenchReport attached to results, merge stats
        if hasattr(agent_fn, "current_error_breakdown"):
            from benchmarks.bench_bfcl import merge_agent_stats, BFCLBenchReport
            bfcl_report = BFCLBenchReport(
                mode=getattr(agent_fn, "mode", "raw"),
                total_tasks=results.total,
                passed_tasks=results.passed,
            )
            merge_agent_stats(bfcl_report, agent_fn)
            # print detailed report
            logger.info("=== BFCL Schema verification report ===")
            logger.info("error classification: %s", bfcl_report.error_breakdown)
            logger.info("retry statistics: %s", bfcl_report.retry_stats)
        return results

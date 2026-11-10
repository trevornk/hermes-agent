"""Durable ownership checks shared by finite one-shot retirement paths."""

from datetime import datetime
from typing import Any


def oneshot_run_inflight(job: dict[str, Any], now: datetime) -> bool:
    """Keep a consumed job available for its owner's fenced terminal write.

    Direct fires need not carry a scheduler run_claim. A fresh fire lease or a live
    claimed/running ledger owner also proves ownership; neither authorizes another dispatch.
    Dead owners and expired leases without a live execution still follow normal retirement.
    """
    from cron import jobs
    from cron.executions import live_inflight_execution

    job_id = job.get('id', '')
    return (
        jobs._job_running_in_this_process(job_id)
        or jobs._claim_is_live(job.get('fire_claim'), now, jobs.FIRE_CLAIM_TTL_SECONDS)
        or live_inflight_execution(job_id) is not None
    )

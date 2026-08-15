from __future__ import annotations

import logging
from datetime import datetime

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from kfcquant.config import SHANGHAI_TZ, Settings
from kfcquant.runtime import write_heartbeat
from kfcquant.services.workflow import Workflow

LOGGER = logging.getLogger(__name__)


def run_scheduler(settings: Settings) -> None:
    workflow = Workflow(settings)
    recovered = workflow.recover_expired_jobs()
    if recovered:
        LOGGER.warning("recovered expired jobs at scheduler startup job_run_ids=%s", ",".join(recovered))
    scheduler = BlockingScheduler(timezone=SHANGHAI_TZ, job_defaults={"coalesce": False, "max_instances": 1})
    schedule = settings.schedule

    def sync_eod() -> None:
        today = datetime.now(SHANGHAI_TZ).date()
        workflow.sync_eod(today, today)

    def preclose() -> None:
        workflow.run_preclose()

    functions = {
        "sync-calendar": workflow.sync_calendar,
        "run-morning": workflow.run_morning,
        "evaluate-morning": workflow.evaluate_morning,
        "run-preclose": preclose,
        "capture-fill": workflow.capture_fill,
        "sync-eod": sync_eod,
        "run-postclose": workflow.run_postclose,
    }
    jobs = [
        (
            functions[command],
            CronTrigger(hour=at.hour, minute=at.minute, timezone=SHANGHAI_TZ),
            command,
        )
        for _, command, at in schedule.scheduled_tasks()
    ]
    jobs.extend(
        (
            workflow.monitor_paper,
            CronTrigger(day_of_week="mon-fri", hour=at.hour, minute=at.minute, timezone=SHANGHAI_TZ),
            f"monitor-paper-{at:%H%M}",
        )
        for at in schedule.monitor_times()
    )
    jobs.append(
        (
            lambda: write_heartbeat(settings),
            CronTrigger(minute=f"*/{schedule.heartbeat_interval_minutes}", timezone=SHANGHAI_TZ),
            "heartbeat",
        )
    )
    for function, trigger, job_id in jobs:
        scheduler.add_job(function, trigger, id=job_id, misfire_grace_time=30, replace_existing=True)
    write_heartbeat(settings)
    scheduler.start()

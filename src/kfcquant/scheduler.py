from __future__ import annotations

from datetime import datetime

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from kfcquant.config import SHANGHAI_TZ, Settings
from kfcquant.runtime import write_heartbeat
from kfcquant.services.workflow import Workflow


def run_scheduler(settings: Settings) -> None:
    workflow = Workflow(settings)
    scheduler = BlockingScheduler(timezone=SHANGHAI_TZ, job_defaults={"coalesce": False, "max_instances": 1})

    def sync_eod() -> None:
        today = datetime.now(SHANGHAI_TZ).date()
        workflow.sync_eod(today, today)

    def preclose() -> None:
        workflow.run_preclose()

    jobs = [
        (workflow.sync_calendar, CronTrigger(hour=8, minute=0, timezone=SHANGHAI_TZ), "sync-calendar"),
        (workflow.run_morning, CronTrigger(hour=8, minute=30, timezone=SHANGHAI_TZ), "run-morning"),
        (workflow.evaluate_morning, CronTrigger(hour=14, minute=35, timezone=SHANGHAI_TZ), "evaluate-morning"),
        (
            workflow.monitor_paper,
            CronTrigger(day_of_week="mon-fri", hour="9-15", minute="*/5", timezone=SHANGHAI_TZ),
            "monitor-paper",
        ),
        (preclose, CronTrigger(hour=14, minute=40, timezone=SHANGHAI_TZ), "run-preclose"),
        (workflow.capture_fill, CronTrigger(hour=14, minute=45, timezone=SHANGHAI_TZ), "capture-fill"),
        (sync_eod, CronTrigger(hour=18, minute=10, timezone=SHANGHAI_TZ), "sync-eod"),
        (workflow.run_postclose, CronTrigger(hour=20, minute=30, timezone=SHANGHAI_TZ), "run-postclose"),
        (lambda: write_heartbeat(settings), CronTrigger(minute="*", timezone=SHANGHAI_TZ), "heartbeat"),
    ]
    for function, trigger, job_id in jobs:
        scheduler.add_job(function, trigger, id=job_id, misfire_grace_time=30, replace_existing=True)
    write_heartbeat(settings)
    scheduler.start()

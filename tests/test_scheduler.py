from __future__ import annotations

from kfcquant import scheduler as scheduler_module


class FakeWorkflow:
    def __init__(self, settings):
        self.settings = settings

    def sync_calendar(self):
        return None

    def run_morning(self):
        return None

    def evaluate_morning(self):
        return None

    def monitor_paper(self):
        return None

    def run_preclose(self):
        return None

    def capture_fill(self):
        return None

    def sync_eod(self, start, end):
        return None

    def run_postclose(self):
        return None

    def recover_expired_jobs(self):
        return []


class FakeScheduler:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.jobs = []
        self.started = False

    def add_job(self, function, trigger, **kwargs):
        self.jobs.append((function, trigger, kwargs))

    def start(self):
        self.started = True


def test_scheduler_builds_triggers_from_schedule_policy(settings, monkeypatch):
    fake = FakeScheduler()
    monkeypatch.setattr(scheduler_module, "Workflow", FakeWorkflow)
    monkeypatch.setattr(scheduler_module, "BlockingScheduler", lambda **kwargs: fake)
    monkeypatch.setattr(scheduler_module, "write_heartbeat", lambda configured: None)

    scheduler_module.run_scheduler(settings)

    jobs = {kwargs["id"]: trigger for _, trigger, kwargs in fake.jobs}
    assert "hour='8', minute='30'" in str(jobs["run-morning"])
    assert "hour='14', minute='40'" in str(jobs["run-preclose"])
    assert "monitor-paper-0930" in jobs
    assert "monitor-paper-1500" in jobs
    assert "minute='*/1'" in str(jobs["heartbeat"])
    assert fake.started


def test_scheduler_recovers_expired_jobs_before_starting(settings, monkeypatch, caplog):
    fake = FakeScheduler()
    monkeypatch.setattr(FakeWorkflow, "recover_expired_jobs", lambda self: ["expired-job"])
    monkeypatch.setattr(scheduler_module, "Workflow", FakeWorkflow)
    monkeypatch.setattr(scheduler_module, "BlockingScheduler", lambda **kwargs: fake)
    monkeypatch.setattr(scheduler_module, "write_heartbeat", lambda configured: None)

    scheduler_module.run_scheduler(settings)

    assert "expired-job" in caplog.text
    assert fake.started

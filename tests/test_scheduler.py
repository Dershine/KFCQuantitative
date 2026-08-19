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


def test_scheduler_fences_overlap_and_does_not_replay_stale_runs(settings, monkeypatch, tmp_path):
    fake = FakeScheduler()
    heartbeat = tmp_path / "worker-heartbeat.json"
    heartbeat.write_text("{}", encoding="utf-8")
    observed = []
    written = []

    def scheduler_factory(**kwargs):
        fake.kwargs = kwargs
        return fake

    monkeypatch.setattr(scheduler_module, "Workflow", FakeWorkflow)
    monkeypatch.setattr(scheduler_module, "BlockingScheduler", scheduler_factory)
    monkeypatch.setattr(scheduler_module, "heartbeat_path", lambda configured: heartbeat)
    monkeypatch.setattr(
        scheduler_module,
        "observe_worker_heartbeat",
        lambda configured, observability: observed.append(configured),
    )
    monkeypatch.setattr(scheduler_module, "write_heartbeat", lambda configured: written.append(configured))

    scheduler_module.run_scheduler(settings)

    assert fake.kwargs["job_defaults"] == {"coalesce": False, "max_instances": 1}
    assert all(kwargs["misfire_grace_time"] == 30 for _, _, kwargs in fake.jobs)
    assert all(kwargs["replace_existing"] is True for _, _, kwargs in fake.jobs)

    functions = {kwargs["id"]: function for function, _, kwargs in fake.jobs}
    functions["heartbeat"]()
    functions["sync-eod"]()
    functions["run-preclose"]()

    assert observed == [settings]
    assert written == [settings, settings]

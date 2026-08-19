from __future__ import annotations

import inspect
from typing import get_type_hints

from kfcquant.application.ports import (
    CandidateEvaluationRepository,
    NewsRepository,
    PortfolioRepository,
    ReportRepository,
)
from kfcquant.application.use_cases import WorkflowUseCases
from kfcquant.db import Database
from kfcquant.repositories import DuckDBRepositories
from kfcquant.services.evaluation import CandidateEvaluationService
from kfcquant.services.news import NewsService
from kfcquant.services.portfolio import PortfolioService
from kfcquant.services.reports import ReportService
from kfcquant.services.workflow import Workflow


def test_workflow_is_a_compatibility_facade_over_single_command_use_cases():
    expected = {
        "doctor",
        "sync_eod",
        "sync_calendar",
        "run_preclose",
        "run_morning",
        "evaluate_morning",
        "evaluate_previous_preclose",
        "capture_fill",
        "monitor_paper",
        "run_postclose",
        "recover_expired_jobs",
    }

    assert set(WorkflowUseCases.__dataclass_fields__) == expected
    for field in WorkflowUseCases.__dataclass_fields__.values():
        use_case = field.type
        public_methods = {
            name
            for name, member in inspect.getmembers(use_case, predicate=inspect.isfunction)
            if not name.startswith("_")
        }
        assert public_methods == {"execute"}

    facade_methods = {
        name
        for name, member in inspect.getmembers(Workflow, predicate=inspect.isfunction)
        if not name.startswith("_")
    }
    assert expected <= facade_methods


def test_duckdb_repository_views_expose_only_their_bounded_context(settings):
    database = Database(settings.database_path, settings.initial_cash)
    database.initialize()
    repositories = DuckDBRepositories(database)

    assert isinstance(repositories.news, NewsRepository)
    assert isinstance(repositories.portfolio, PortfolioRepository)
    assert isinstance(repositories.evaluation, CandidateEvaluationRepository)
    assert isinstance(repositories.report, ReportRepository)
    assert hasattr(repositories.portfolio, "get_cash")
    assert not hasattr(repositories.portfolio, "save_report")
    assert hasattr(repositories.news, "pending_news_documents")
    assert not hasattr(repositories.news, "apply_buy_fill")


def test_services_depend_on_minimal_repository_ports_instead_of_database():
    expected = {
        NewsService: NewsRepository,
        PortfolioService: PortfolioRepository,
        CandidateEvaluationService: CandidateEvaluationRepository,
        ReportService: ReportRepository,
    }
    for service, repository_port in expected.items():
        hints = get_type_hints(service.__init__)
        assert hints["repository"] is repository_port
        assert "database" not in inspect.signature(service.__init__).parameters

from __future__ import annotations

import ast
import importlib
from pathlib import Path
from unittest.mock import Mock

from kfcquant.bootstrap import build_research_application
from kfcquant.db import Database

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
    return imported


def test_workflow_facade_uses_the_composition_root_instead_of_constructing_infrastructure():
    bootstrap = importlib.import_module("kfcquant.bootstrap")
    workflow_path = PROJECT_ROOT / "src" / "kfcquant" / "services" / "workflow.py"
    imports = _imports(workflow_path)

    assert callable(bootstrap.build_research_application)
    assert "kfcquant.bootstrap" in imports
    assert "kfcquant.db" not in imports
    assert "kfcquant.providers.factory" not in imports
    assert "kfcquant.repositories" not in imports


def test_dashboard_depends_on_an_explicit_query_model_instead_of_database_tables():
    queries = importlib.import_module("kfcquant.application.queries")
    dashboard_path = PROJECT_ROOT / "src" / "kfcquant" / "dashboard.py"
    source = dashboard_path.read_text(encoding="utf-8")
    imports = _imports(dashboard_path)

    assert hasattr(queries, "DashboardQueryModel")
    assert "kfcquant.bootstrap" in imports
    assert "kfcquant.db" not in imports
    assert "database." not in source
    assert ".table(" not in source
    assert ".table_with_strategy(" not in source


def test_composition_root_preserves_explicit_test_injections(settings):
    database = Database(settings.database_path, settings.initial_cash)
    market = Mock()
    live = Mock()
    news = Mock()
    llm = Mock()

    application = build_research_application(
        settings,
        database=database,
        market_provider=market,
        live_provider=live,
        news_provider=news,
        llm_provider=llm,
    )

    assert application.database is database
    assert application.market_provider is market
    assert application.live_provider is live
    assert application.news_provider is news
    assert application.llm_provider is llm
    assert application.use_cases.run_morning.strategy_registry is application.strategy_registry
    assert application.use_cases.run_preclose.portfolio is application.portfolio

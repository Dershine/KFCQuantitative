from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from uuid import uuid4

from kfcquant.db import Database
from kfcquant.interfaces import LLMProvider


class ReportService:
    def __init__(self, database: Database, llm: LLMProvider | None, report_dir: Path, model_name: str):
        self.database = database
        self.llm = llm
        self.report_dir = report_dir
        self.model_name = model_name

    @staticmethod
    def _fallback(context: dict[str, object]) -> str:
        candidates = context.get("candidates", [])
        after_entry = context.get("after_entry_events", [])
        return "\n".join(
            [
                f"# {context['report_date']} 尾盘机会研究复盘",
                "",
                "> 本报告仅记录量化研究与影子组合，不构成投资建议。",
                "",
                f"- 14:40候选数：{len(candidates)}",
                f"- 入场后新增风险事件：{len(after_entry)}",
                f"- 影子组合现金：{context.get('cash', 'N/A')}",
                f"- 当前持仓数：{len(context.get('positions', []))}",
                "",
                "收盘后未知公告无法由14:40系统提前预测，属于不可消除的隔夜风险。",
            ]
        )

    def generate(self, report_date: date, generated_at: datetime, context: dict[str, object]) -> str:
        model_name = self.model_name
        if self.llm:
            try:
                content = self.llm.generate_report(context)
            except Exception as exc:
                content = self._fallback(context) + f"\n\n模型报告降级：{exc}"
                model_name = "deterministic-fallback"
        else:
            content = self._fallback(context)
            model_name = "deterministic-fallback"
        self.report_dir.mkdir(parents=True, exist_ok=True)
        path = self.report_dir / f"{report_date.isoformat()}-postclose.md"
        path.write_text(content, encoding="utf-8")
        self.database.save_report(str(uuid4()), report_date, generated_at, "postclose", content, model_name)
        return content

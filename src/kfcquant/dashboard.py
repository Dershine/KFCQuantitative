from __future__ import annotations

import json
from datetime import datetime

import pandas as pd
import plotly.express as px
import streamlit as st
from filelock import Timeout

from kfcquant.config import SHANGHAI_TZ, get_settings
from kfcquant.db import Database
from kfcquant.models import SignalKind

st.set_page_config(page_title="KFCQuant 双时段研究", page_icon="📈", layout="wide")
settings = get_settings()
database = Database(
    settings.database_path,
    settings.initial_cash,
    settings.database_lock_timeout_seconds,
    settings.runtime_dir / "database.lock",
)
if not settings.database_read_only:
    database.initialize()

st.title("A股双时段机会研究 Agent")
st.warning("仅用于个人量化研究与影子组合，不连接券商，不构成投资建议。收盘后未知公告属于不可消除的隔夜风险。")


def safe_table(name: str, limit: int = 1000) -> pd.DataFrame:
    try:
        return database.table(name, limit)
    except Timeout:
        st.info("研究任务正在更新数据，请稍后刷新页面。")
        return pd.DataFrame()
    except Exception as exc:
        st.error(f"读取 {name} 失败：{exc}")
        return pd.DataFrame()


def safe_read(operation, default):
    try:
        return operation()
    except Timeout:
        st.info("研究任务正在更新数据，请稍后刷新页面。")
        return default


tabs = st.tabs(["今日候选", "候选详情", "影子组合", "交易记录", "前向评估", "数据健康", "收盘报告"])

with tabs[0]:
    for title, kind in (
        (f"{settings.schedule.morning_run_at:%H:%M} 盘前观察名单", SignalKind.MORNING_WATCHLIST.value),
        (f"{settings.schedule.preclose_run_at:%H:%M} 尾盘入场候选", SignalKind.PRECLOSE_ENTRY.value),
    ):
        st.subheader(title)
        run = safe_read(
            lambda signal_kind=kind: database.latest_signal_run(
                datetime.now(SHANGHAI_TZ).date(), signal_kind
            ),
            None,
        )
        if not run:
            st.info(f"今天尚未运行{title[:5]}信号。")
            continue
        status_cols = st.columns(5)
        status_cols[0].metric("运行状态", run["status"])
        status_cols[1].metric("候选数", int(run["candidate_count"]))
        status_cols[2].metric("可模拟成交", "是" if run["tradable"] else "否")
        status_cols[3].metric("行情新鲜", "是" if run["data_fresh"] else "否")
        status_cols[4].metric("公告源健康", "是" if run["official_news_healthy"] else "否")
        st.caption(
            f"信号时间：{run['as_of']}｜信息截止：{run['information_cutoff']}｜"
            f"策略：{run['strategy_version']}｜{run['message']}"
        )
        if settings.data_profile == "learning":
            st.caption("学习模式公告来自AKShare的东方财富公告镜像；页面保留实际来源，不等同于付费官方直连。")
        candidates = safe_read(
            lambda signal_run=run: database.get_candidates(
                str(signal_run["run_id"]), include_blocked=True
            ),
            pd.DataFrame(),
        )
        if candidates.empty:
            st.info("没有满足条件的候选。")
        else:
            top = settings.selection.select_frame(candidates)
            top["风险状态"] = top["blocked"].map({True: "已排除", False: "通过"})
            st.dataframe(
                top[["rank", "ts_code", "name", "opportunity_score", "风险状态", "quote_at"]],
                width="stretch",
                hide_index=True,
            )
            chart = px.bar(
                top,
                x="ts_code",
                y="opportunity_score",
                color="风险状态",
                title=f"前{settings.selection.top_n}名机会评分（非上涨概率）",
            )
            st.plotly_chart(chart, width="stretch")

with tabs[1]:
    selected_kind = st.radio(
        "信号类型",
        [SignalKind.MORNING_WATCHLIST.value, SignalKind.PRECLOSE_ENTRY.value],
        format_func=lambda value: (
            f"{settings.schedule.morning_run_at:%H:%M}盘前观察"
            if value == SignalKind.MORNING_WATCHLIST.value
            else f"{settings.schedule.preclose_run_at:%H:%M}尾盘入场"
        ),
        horizontal=True,
    )
    run = safe_read(lambda: database.latest_signal_run(signal_kind=selected_kind), None)
    if not run:
        st.info("暂无候选记录。")
    else:
        candidates = safe_read(
            lambda: database.get_candidates(str(run["run_id"]), include_blocked=True), pd.DataFrame()
        )
        if candidates.empty:
            st.info("暂无候选记录。")
        else:
            options = {
                f"#{int(row['rank'])} {row['ts_code']} {row['name']}": row
                for row in candidates.head(settings.selection.candidate_limit).to_dict("records")
            }
            selected = st.selectbox("选择股票", list(options))
            row = options[selected]
            left, right = st.columns([1, 2])
            left.metric("机会评分", f"{row['opportunity_score']:.2f}")
            left.metric("风险过滤", "已排除" if row["blocked"] else "通过")
            left.caption(f"实时快照：{row['quote_at']}")
            factors = json.loads(row["factor_json"])
            factor_frame = pd.DataFrame({"指标": list(factors), "数值": list(factors.values())})
            right.dataframe(factor_frame, width="stretch", hide_index=True)
            reasons = json.loads(row["block_reasons_json"])
            if reasons:
                st.error("；".join(reasons))
            event_ids = json.loads(row["risk_event_ids_json"])
            events = safe_table("risk_events")
            if event_ids and not events.empty:
                st.subheader("关联资讯证据")
                st.dataframe(
                    events[events["event_id"].isin(event_ids)][
                        ["published_at", "event_type", "severity", "confidence", "evidence", "source_url"]
                    ],
                    width="stretch",
                    hide_index=True,
                )

with tabs[2]:
    cash = safe_read(database.get_cash, 0.0)
    positions = safe_read(database.get_open_positions, pd.DataFrame())
    cols = st.columns(3)
    cols[0].metric("可用现金", f"¥{cash:,.2f}")
    cols[1].metric("当前持仓", len(positions))
    cols[2].metric("最多持仓", settings.max_positions)
    if positions.empty:
        st.info("当前没有影子持仓。")
    else:
        quotes = safe_read(
            database.get_latest_quotes,
            pd.DataFrame(columns=["ts_code", "price", "captured_at"]),
        )
        display = positions.merge(quotes[["ts_code", "price", "captured_at"]], on="ts_code", how="left")
        display["market_value"] = display["shares"] * display["price"]
        display["unrealized_pnl"] = display["shares"] * (display["price"] - display["cost_basis"])
        st.dataframe(display, width="stretch", hide_index=True)

with tabs[3]:
    orders = safe_read(lambda: database.table_with_strategy("paper_orders"), pd.DataFrame())
    fills = safe_table("paper_fills")
    st.subheader("订单")
    st.dataframe(orders, width="stretch", hide_index=True)
    st.subheader("成交")
    st.dataframe(fills, width="stretch", hide_index=True)

with tabs[4]:
    st.subheader("候选信号前向评估")
    morning_outcomes = safe_read(
        lambda: database.candidate_outcomes(SignalKind.MORNING_WATCHLIST.value), pd.DataFrame()
    )
    preclose_outcomes = safe_read(
        lambda: database.candidate_outcomes(SignalKind.PRECLOSE_ENTRY.value), pd.DataFrame()
    )
    outcome_cols = st.columns(2)
    for column, title, frame in (
        (outcome_cols[0], f"{settings.schedule.morning_run_at:%H:%M} 当日命中", morning_outcomes),
        (outcome_cols[1], f"{settings.schedule.preclose_run_at:%H:%M} 次日命中", preclose_outcomes),
    ):
        with column:
            evaluable = frame[frame["status"].isin(["hit", "miss"])] if not frame.empty else frame
            value = f"{(evaluable['status'] == 'hit').mean():.1%}" if not evaluable.empty else "样本不足"
            st.metric(title, value)
            if not frame.empty:
                st.dataframe(frame.head(50), width="stretch", hide_index=True)
    st.subheader("影子组合交易评估")
    outcomes = safe_read(lambda: database.table_with_strategy("opportunity_outcomes"), pd.DataFrame())
    if outcomes.empty:
        st.info("尚无已完成持仓。概率校准至少需要60个交易日的前向样本。")
    else:
        metrics = st.columns(5)
        metrics[0].metric("完成交易", len(outcomes))
        metrics[1].metric("次日1.5%命中", f"{outcomes['first_day_hit'].mean():.1%}")
        metrics[2].metric("5日命中", f"{outcomes['five_day_hit'].mean():.1%}")
        metrics[3].metric("平均净收益", f"{outcomes['net_return'].mean():.2%}")
        cumulative = (1 + outcomes.sort_values("recorded_at")["net_return"]).cumprod()
        metrics[4].metric("观测期累计", f"{cumulative.iloc[-1] - 1:.2%}")
        curve = pd.DataFrame({"交易序号": range(1, len(cumulative) + 1), "累计净值": cumulative.values})
        st.plotly_chart(px.line(curve, x="交易序号", y="累计净值", title="前向影子交易序列"), width="stretch")
        st.caption("此处是前向观察结果，不是历史回测，也不代表未来表现。")

with tabs[5]:
    jobs = safe_table("job_runs", 200)
    runs = safe_read(lambda: database.recent_signal_runs(100), pd.DataFrame())
    documents = safe_table("news_documents", 200)
    if not jobs.empty:
        st.subheader("任务运行")
        st.dataframe(jobs, width="stretch", hide_index=True)
    if not runs.empty:
        st.subheader("信号运行")
        st.dataframe(runs, width="stretch", hide_index=True)
    if not documents.empty:
        status_counts = documents["processing_status"].value_counts().rename_axis("状态").reset_index(name="数量")
        st.subheader("资讯处理状态")
        st.dataframe(status_counts, width="stretch", hide_index=True)

with tabs[6]:
    reports = safe_table("reports", 60)
    if reports.empty:
        st.info(f"尚未生成{settings.schedule.postclose_at:%H:%M}收盘报告。")
    else:
        report = reports.sort_values("generated_at", ascending=False).iloc[0]
        st.caption(f"生成时间：{report['generated_at']}｜模型：{report['model_name']}")
        st.markdown(report["content"])

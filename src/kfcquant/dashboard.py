from __future__ import annotations

import json
from datetime import datetime

import pandas as pd
import plotly.express as px
import streamlit as st
from filelock import Timeout

from kfcquant.application.queries import (
    DataHealthProjection,
    EvaluationProjection,
    PortfolioProjection,
    TradingActivityProjection,
)
from kfcquant.bootstrap import build_dashboard_query_model
from kfcquant.config import SHANGHAI_TZ, get_settings
from kfcquant.models import SignalKind

st.set_page_config(page_title="KFCQuant 双时段研究", page_icon="📈", layout="wide")
settings = get_settings()
query_model = build_dashboard_query_model(settings)

st.title("A股双时段机会研究 Agent")
st.warning("仅用于个人量化研究与影子组合，不连接券商，不构成投资建议。收盘后未知公告属于不可消除的隔夜风险。")


def safe_read(operation, default):
    try:
        return operation()
    except Timeout:
        st.info("研究任务正在更新数据，请稍后刷新页面。")
        return default
    except Exception as exc:
        st.error(f"读取数据失败：{exc}")
        return default


tabs = st.tabs(["今日候选", "候选详情", "影子组合", "交易记录", "前向评估", "数据健康", "收盘报告"])

with tabs[0]:
    today = datetime.now(SHANGHAI_TZ).date()
    for title, kind, job_name in (
        (
            f"{settings.schedule.morning_run_at:%H:%M} 盘前观察名单",
            SignalKind.MORNING_WATCHLIST,
            "run-morning",
        ),
        (
            f"{settings.schedule.preclose_run_at:%H:%M} 尾盘入场候选",
            SignalKind.PRECLOSE_ENTRY,
            "run-preclose",
        ),
    ):
        st.subheader(title)
        signal = safe_read(
            lambda signal_kind=kind: query_model.latest_signal(
                signal_kind,
                today,
            ),
            None,
        )
        if signal is None:
            job = safe_read(
                lambda current_job=job_name: query_model.latest_job(current_job, today),
                None,
            )
            if job and job["status"] == "failed":
                st.error(
                    f"今天的{title}运行失败（{job['finished_at'] or job['started_at']}）：{job['message']}"
                )
            elif job and job["status"] == "missed":
                st.warning(f"今天的{title}已错过：{job['message']}")
            elif job and job["status"] == "running":
                st.warning(f"今天的{title}正在运行，开始时间：{job['started_at']}")
            else:
                st.info(f"今天尚未运行{title[:5]}信号。")
            continue
        run = signal.run
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
        candidates = signal.candidates
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
    signal = safe_read(lambda: query_model.latest_signal(SignalKind(selected_kind)), None)
    if signal is None:
        st.info("暂无候选记录。")
    else:
        candidates = signal.candidates
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
            events = safe_read(lambda: query_model.risk_events(event_ids), pd.DataFrame())
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
    portfolio = safe_read(query_model.portfolio, PortfolioProjection(0.0, pd.DataFrame()))
    cash = portfolio.cash
    positions = portfolio.positions
    cols = st.columns(3)
    cols[0].metric("可用现金", f"¥{cash:,.2f}")
    cols[1].metric("当前持仓", len(positions))
    cols[2].metric("最多持仓", settings.max_positions)
    if positions.empty:
        st.info("当前没有影子持仓。")
    else:
        st.dataframe(positions, width="stretch", hide_index=True)

with tabs[3]:
    activity = safe_read(
        query_model.trading_activity,
        TradingActivityProjection(pd.DataFrame(), pd.DataFrame()),
    )
    st.subheader("订单")
    st.dataframe(activity.orders, width="stretch", hide_index=True)
    st.subheader("成交")
    st.dataframe(activity.fills, width="stretch", hide_index=True)

with tabs[4]:
    evaluations = safe_read(
        query_model.evaluations,
        EvaluationProjection(pd.DataFrame(), pd.DataFrame(), pd.DataFrame()),
    )
    st.subheader("候选信号前向评估")
    outcome_cols = st.columns(2)
    for column, title, frame in (
        (
            outcome_cols[0],
            f"{settings.schedule.morning_run_at:%H:%M} 当日命中",
            evaluations.morning_candidates,
        ),
        (
            outcome_cols[1],
            f"{settings.schedule.preclose_run_at:%H:%M} 次日命中",
            evaluations.preclose_candidates,
        ),
    ):
        with column:
            evaluable = frame[frame["status"].isin(["hit", "miss"])] if not frame.empty else frame
            value = f"{(evaluable['status'] == 'hit').mean():.1%}" if not evaluable.empty else "样本不足"
            st.metric(title, value)
            if not frame.empty:
                st.dataframe(frame.head(50), width="stretch", hide_index=True)
    st.subheader("影子组合交易评估")
    outcomes = evaluations.opportunities
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
    health = safe_read(
        query_model.data_health,
        DataHealthProjection(pd.DataFrame(), pd.DataFrame(), pd.DataFrame()),
    )
    if not health.jobs.empty:
        st.subheader("任务运行")
        st.dataframe(health.jobs, width="stretch", hide_index=True)
    if not health.runs.empty:
        st.subheader("信号运行")
        st.dataframe(health.runs, width="stretch", hide_index=True)
    if not health.news_status_counts.empty:
        status_counts = health.news_status_counts.rename(columns={"status": "状态", "count": "数量"})
        st.subheader("资讯处理状态")
        st.dataframe(status_counts, width="stretch", hide_index=True)

with tabs[6]:
    report = safe_read(query_model.latest_report, None)
    if report is None:
        st.info(f"尚未生成{settings.schedule.postclose_at:%H:%M}收盘报告。")
    else:
        st.caption(f"生成时间：{report['generated_at']}｜模型：{report['model_name']}")
        st.markdown(report["content"])

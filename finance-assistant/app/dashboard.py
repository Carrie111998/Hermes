from __future__ import annotations

from datetime import date

import plotly.express as px
import streamlit as st

from app.analysis.service import AnalysisService
from app.main import DB_PATH, ROOT


def _months() -> list[str]:
    today = date.today()
    values = []
    for offset in range(12):
        index = today.year * 12 + today.month - 1 - offset
        values.append(f"{index // 12:04d}-{index % 12 + 1:02d}")
    return values


def render_dashboard(service: AnalysisService) -> None:
    st.title("Local finance dashboard")
    month = st.selectbox("Month", _months())
    analysis = service.analyze(month)
    month_index = int(analysis.period[:4]), int(analysis.period[5:])
    previous_index = month_index[0] * 12 + month_index[1] - 2
    previous_month = f"{previous_index // 12:04d}-{previous_index % 12 + 1:02d}"
    comparison = service.compare_months(month, previous_month)
    st.caption("Read-only aggregate view; raw merchant and card data are not displayed.")
    columns = st.columns(4)
    columns[0].metric("Total spending", f"{analysis.total_spending:.2f}", f"{comparison.difference:.2f} vs {previous_month}")
    columns[1].metric("Transactions", analysis.transaction_count)
    columns[2].metric("Uncategorized", analysis.uncategorized_count)
    columns[3].metric("Refunds", f"{analysis.refund_total:.2f}")

    warnings = [f"{item.bank}: {item.status}" for item in analysis.statement_completeness
                if item.status != "PRESENT"]
    if warnings:
        st.warning("Statement completeness: " + "; ".join(warnings))

    left, right = st.columns(2)
    with left:
        st.subheader("By category")
        category = px.bar(x=list(analysis.by_category), y=[float(v) for v in analysis.by_category.values()])
        st.plotly_chart(category, use_container_width=True)
    with right:
        st.subheader("By bank")
        bank = px.bar(x=list(analysis.by_bank), y=[float(v) for v in analysis.by_bank.values()])
        st.plotly_chart(bank, use_container_width=True)

    st.subheader("Monthly trend")
    trend = service.trend(month, 6)
    trend_chart = px.line(x=[item.period for item in trend], y=[float(item.total_spending) for item in trend])
    st.plotly_chart(trend_chart, use_container_width=True)

    st.subheader("Uncategorized transactions")
    st.info(
        f"{analysis.uncategorized_count} transaction(s) need categorization. "
        "Individual transaction details are intentionally hidden in this aggregate dashboard."
    )


def main() -> None:
    render_dashboard(AnalysisService.from_path(DB_PATH, ROOT / "config"))


if __name__ == "__main__":
    main()

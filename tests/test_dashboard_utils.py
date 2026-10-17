"""
Unit tests for src/dashboard_utils.py.

These exist because src/app.py itself is a top-to-bottom Streamlit script
and can't be meaningfully unit tested without a running Streamlit session.
Pulling the KPI math and Q&A context-building logic out into pure functions
makes them directly testable here, and this is also where the "Q&A assistant
should never silently drop the transaction being asked about" fix is proven.

Run with: pytest tests/test_dashboard_utils.py -v
"""
import pandas as pd
import pytest

from src.dashboard_utils import (
    build_qna_context,
    compute_kpis,
    extract_mentioned_ids,
    fallback_was_used,
)


def make_audit_df():
    return pd.DataFrame({
        "Transaction ID": [1001, 1002, 1003, 1004],
        "Discrepancy (₹)": [-500.0, 0.0, 1500.0, -50.0],
        "Agent Recommended Action": ["IGNORE", "IGNORE", "ESCALATE", "IGNORE"],
        "Actual Planted Error": ["fee_double_charged", "None (Valid Variance)", "MISSING_IN_GATEWAY", "None (Valid Variance)"],
        "confidence": [0.0, 0.0, 0.9, 0.0],
    })


def test_compute_kpis_basic_recovery_math():
    kpis = compute_kpis(make_audit_df())
    # Real leaks: txn 1001 (500) and txn 1003 (1500) => total at risk 2000
    assert kpis["total_at_risk"] == 2000.0
    # Of those, only txn 1003 was escalated => recovered 1500
    assert kpis["recovered"] == 1500.0
    assert kpis["recovery_pct"] == pytest.approx(75.0)
    # txn 1001 was a real leak but not escalated => 1 missed leak
    assert kpis["missed_leaks"] == 1
    assert kpis["total_exceptions"] == 4


def test_compute_kpis_empty_dataframe_does_not_crash():
    kpis = compute_kpis(pd.DataFrame())
    assert kpis["total_at_risk"] == 0.0
    assert kpis["total_exceptions"] == 0


def test_compute_kpis_supports_legacy_column_names():
    legacy_df = pd.DataFrame({
        "Transaction ID": [1],
        "variance_inr": [100.0],
        "Agent Recommended Action": ["ESCALATE"],
    })
    kpis = compute_kpis(legacy_df)
    assert kpis["total_at_risk"] == 100.0
    assert kpis["recovered"] == 100.0


def test_fallback_was_used_detects_zero_confidence():
    assert fallback_was_used(make_audit_df()) is True
    clean_df = make_audit_df()
    clean_df["confidence"] = [0.8, 0.9, 0.95, 0.7]
    assert fallback_was_used(clean_df) is False


# ---------------------------------------------------------------------------
# extract_mentioned_ids / build_qna_context -- the hallucination-prevention fix
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("question,expected_ids", [
    ("Why was TXN_1179 escalated?", ["1179"]),
    ("why was txn-1179 escalated", ["1179"]),
    ("what about transaction 1179?", ["1179"]),
    ("compare 1001 and 1003", ["1001", "1003"]),
    ("how much money are we losing overall", []),
])
def test_extract_mentioned_ids(question, expected_ids):
    assert extract_mentioned_ids(question) == expected_ids


def test_build_qna_context_always_includes_explicitly_mentioned_transaction():
    # Regression test for the bug where the Streamlit Q&A assistant only ever
    # saw audit_df.head(25): a question about a transaction outside that
    # window had no way to be answered correctly and could invite a guess.
    rows = []
    for i in range(100):
        rows.append({"Transaction ID": 2000 + i, "Discrepancy (₹)": 1.0})
    # The transaction the user asks about is row index 99 -- far outside any
    # naive head(25)/head(40) slice, and has the smallest discrepancy so it
    # would not be picked up by a "top N by risk" slice either.
    rows[99] = {"Transaction ID": 2099, "Discrepancy (₹)": 0.0}
    df = pd.DataFrame(rows)

    context = build_qna_context(df, "Why was TXN_2099 escalated?", top_n=15, max_total_rows=40)
    assert 2099 in context["Transaction ID"].astype(int).values


def test_build_qna_context_falls_back_to_top_n_for_general_questions():
    rows = [{"Transaction ID": 3000 + i, "Discrepancy (₹)": float(i)} for i in range(50)]
    df = pd.DataFrame(rows)
    context = build_qna_context(df, "what's our biggest risk right now?", top_n=5, max_total_rows=40)
    # Highest discrepancy rows (49..45) should be present
    assert 3049 in context["Transaction ID"].values
    assert len(context) <= 5


def test_build_qna_context_handles_empty_dataframe():
    assert build_qna_context(pd.DataFrame(), "anything", top_n=5).empty
"""
Pure helper functions used by the Streamlit dashboard (src/app.py).

Kept separate from app.py so this logic can be unit tested without needing
Streamlit itself to be importable (see tests/test_dashboard_utils.py) --
Streamlit apps are otherwise awkward to unit test because the module runs
top-to-bottom as a script.
"""
import re
import pandas as pd


def resolve_discrepancy_column(df: pd.DataFrame) -> str:
    return "Discrepancy (₹)" if "Discrepancy (₹)" in df.columns else "variance_inr"


def resolve_action_column(df: pd.DataFrame) -> str:
    return "recommended_action" if "recommended_action" in df.columns else "Agent Recommended Action"


def real_leak_mask(df: pd.DataFrame) -> pd.Series:
    if "Actual Planted Error" in df.columns:
        return df["Actual Planted Error"] != "None (Valid Variance)"
    return pd.Series([True] * len(df), index=df.index)


def escalated_mask(df: pd.DataFrame, action_col: str) -> pd.Series:
    if action_col not in df.columns:
        return pd.Series([True] * len(df), index=df.index)
    return df[action_col].astype(str).str.contains("ESCALATE", case=False, na=False)


def compute_kpis(df: pd.DataFrame) -> dict:
    """Returns the headline KPI numbers shown at the top of the dashboard."""
    if df.empty:
        return {"total_at_risk": 0.0, "recovered": 0.0, "recovery_pct": 0.0,
                "missed_leaks": 0, "total_exceptions": 0}

    discrepancy_col = resolve_discrepancy_column(df)
    if discrepancy_col not in df.columns:
        df = df.copy()
        df[discrepancy_col] = 0.0

    action_col = resolve_action_column(df)
    leak_mask = real_leak_mask(df)
    esc_mask = escalated_mask(df, action_col)

    total_at_risk = float(df.loc[leak_mask, discrepancy_col].abs().sum())
    recovered = float(df.loc[leak_mask & esc_mask, discrepancy_col].abs().sum())
    recovery_pct = (recovered / total_at_risk * 100) if total_at_risk > 0 else 0.0
    missed_leaks = int(leak_mask.sum() - (leak_mask & esc_mask).sum())

    return {
        "total_at_risk": total_at_risk,
        "recovered": recovered,
        "recovery_pct": recovery_pct,
        "missed_leaks": missed_leaks,
        "total_exceptions": len(df),
    }


def fallback_was_used(df: pd.DataFrame) -> bool:
    if "confidence" in df.columns:
        return bool((df["confidence"] == 0.0).any())
    if "Used LLM" in df.columns:
        return bool((~df["Used LLM"]).any())
    return False


# Matches "TXN_1179", "TXN-1179", "txn1179", or a bare "1179" (3+ digits, to
# avoid matching small numbers like "2" or a year fragment).
TXN_ID_PATTERN = re.compile(r"\b(?:TXN[_-]?)?(\d{3,})\b", re.IGNORECASE)


def extract_mentioned_ids(text: str) -> list:
    """Pulls plausible transaction IDs out of a free-text question, e.g.
    'Why was TXN_1179 escalated?' or 'what about transaction 1179?'."""
    if not text:
        return []
    return list(dict.fromkeys(TXN_ID_PATTERN.findall(text)))


def build_qna_context(df: pd.DataFrame, user_question: str, id_col: str = "Transaction ID",
                       top_n: int = 15, max_total_rows: int = 40) -> pd.DataFrame:
    """
    Builds a token-bounded but question-aware slice of the audit sheet to
    ground the Q&A assistant:
      1. every row whose transaction ID is explicitly mentioned in the
         question, so asking about a specific transaction never silently
         gets truncated away, plus
      2. the top `top_n` rows by absolute discrepancy, to cover general
         "what's our biggest risk" style questions.

    This replaces a blind `.head(25)` / positional slice, which could omit
    the exact row a question was asking about and invite the model to guess
    instead of saying "I don't see that transaction."
    """
    if df.empty or id_col not in df.columns:
        return df.head(top_n)

    discrepancy_col = resolve_discrepancy_column(df)
    mentioned_ids = extract_mentioned_ids(user_question)

    if mentioned_ids:
        mentioned_rows = df[df[id_col].astype(str).isin(mentioned_ids)]
    else:
        mentioned_rows = df.iloc[0:0]

    if discrepancy_col in df.columns:
        order = df[discrepancy_col].abs().sort_values(ascending=False).index
        top_rows = df.reindex(order).head(top_n)
    else:
        top_rows = df.head(top_n)

    context = pd.concat([mentioned_rows, top_rows]).drop_duplicates(subset=[id_col])
    return context.head(max_total_rows)

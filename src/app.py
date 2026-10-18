import streamlit as st
import pandas as pd
import json
import os
from pathlib import Path

from dotenv import load_dotenv
from groq import Groq

from dashboard_utils import (
    build_qna_context,
    compute_kpis,
    fallback_was_used,
    resolve_action_column,
    resolve_discrepancy_column,
)

load_dotenv()

st.set_page_config(page_title="LedgerLens — Finance Reconciliation Controller", page_icon="📒", layout="wide")

ROOT = Path(__file__).resolve().parent.parent


@st.cache_data
def load_data():
    audit_path = ROOT / "output" / "reconciliation_audit_sheet.csv"
    if not audit_path.exists():
        return pd.DataFrame(), {}
    audit_df = pd.read_csv(audit_path)
    with open(ROOT / "contracts" / "ochicken_contract.json", "r") as f:
        contract = json.load(f)
    return audit_df, contract


audit_df, contract = load_data()

st.title("📒 LedgerLens — AI-Assisted Finance Reconciliation Controller")
st.caption("Deterministic bank ↔ gateway reconciliation, with AI used only to explain and triage exceptions "
           "— never to decide the numbers.")
st.markdown("---")

if audit_df.empty:
    st.warning("⚠️ No audit sheet found. Run `python main.py` first to execute the reconciliation pipeline.")
    st.stop()

discrepancy_col = resolve_discrepancy_column(audit_df)
if discrepancy_col not in audit_df.columns:
    audit_df[discrepancy_col] = 0.0
action_col = resolve_action_column(audit_df)

kpis = compute_kpis(audit_df)

col1, col2, col3, col4 = st.columns(4)
with col1:
    st.metric("Total Capital at Risk", f"₹{kpis['total_at_risk']:,.2f}")
with col2:
    st.metric("Capital Recovered", f"₹{kpis['recovered']:,.2f}", delta=f"{kpis['recovery_pct']:.1f}% Recovery")
with col3:
    st.metric("Real Leaks Missed", kpis["missed_leaks"],
               delta=None if kpis["missed_leaks"] == 0 else "Needs attention", delta_color="inverse")
with col4:
    st.metric("Actionable Exceptions", kpis["total_exceptions"])

if fallback_was_used(audit_df):
    st.warning("⚠️ One or more exceptions used the deterministic fallback heuristic (no live LLM call). "
               "Root-cause text for those rows is a generic placeholder, not an AI diagnosis — "
               "set GROQ_API_KEY to enable live diagnostics. See the 'confidence' / 'Used LLM' column.")

st.markdown("---")

# --- Operator Action Card -----------------------------------------------
st.subheader("⚡ Operator Action Queue (Highest Risk Triage)")
high_risk_subset = audit_df.sort_values(by=discrepancy_col, key=abs, ascending=False)

if not high_risk_subset.empty:
    top_txn = high_risk_subset.iloc[0]
    txn_id = top_txn.get("transaction_id", top_txn.get("Transaction ID", "TXN_UNKNOWN"))
    variance_amt = abs(float(top_txn.get(discrepancy_col, 0.0)))
    cause_desc = top_txn.get("root_cause", top_txn.get("Agent Diagnosis", "Discrepancy detected"))
    rec_action = top_txn.get("recommended_action", top_txn.get("Agent Recommended Action", "ESCALATE TO HUMAN REVIEW"))
    confidence_score = top_txn.get("confidence", top_txn.get("Agent Confidence", 1.0))
    try:
        confidence_score = float(confidence_score)
    except (TypeError, ValueError):
        confidence_score = 1.0

    with st.container(border=True):
        t_col1, t_col2 = st.columns([3, 1])
        with t_col1:
            st.markdown(f"#### 🚨 Highest Risk Item: `{txn_id}` — **₹{variance_amt:,.2f} Variance**")
            st.markdown(f"**Root Cause Diagnosis:** {cause_desc}")
            st.markdown(f"**Action Plan:** `{rec_action}` (Confidence: {confidence_score:.0%})")
        with t_col2:
            st.write("")
            if st.button("✅ Approve Dispute Ticket", use_container_width=True):
                st.success(f"Dispute initiated for `{txn_id}` with Razorpay Support.")
            if st.button("⚠️ Dismiss / Mark Cleared", use_container_width=True):
                st.info(f"`{txn_id}` marked as cleared.")

st.markdown("---")

# --- Filterable, selectable audit table ----------------------------------
st.subheader("📊 Complete Audit Sheet")

f_col1, f_col2, f_col3 = st.columns([2, 1, 1])
with f_col1:
    search_term = st.text_input("🔍 Search by Transaction ID, Cause, or Recommendation:", "")
with f_col2:
    detection_rule_col = "Detection Rule" if "Detection Rule" in audit_df.columns else "exception_type"
    rule_options = sorted(audit_df[detection_rule_col].dropna().unique()) if detection_rule_col in audit_df.columns else []
    selected_rules = st.multiselect("Filter by Detection Rule", rule_options, default=[])
with f_col3:
    approval_col = "Human Approval Needed" if "Human Approval Needed" in audit_df.columns else "human_approval_required"
    approval_filter = st.selectbox("Human Approval Needed", ["All", "Yes", "No"])

filtered_df = audit_df.copy()
if search_term:
    filtered_df = filtered_df[filtered_df.astype(str).apply(lambda x: x.str.contains(search_term, case=False)).any(axis=1)]
if selected_rules and detection_rule_col in filtered_df.columns:
    filtered_df = filtered_df[filtered_df[detection_rule_col].isin(selected_rules)]
if approval_filter != "All" and approval_col in filtered_df.columns:
    want = approval_filter == "Yes"
    filtered_df = filtered_df[filtered_df[approval_col].astype(bool) == want]

# Sortable natively by clicking any column header in st.dataframe; this
# selectbox additionally offers an explicit "sort by" control for anyone who
# prefers it to column-header clicking.
sortable_cols = [c for c in [discrepancy_col, "Transaction ID", detection_rule_col] if c in filtered_df.columns]
sort_col = st.selectbox("Sort by", sortable_cols, index=0) if sortable_cols else None
if sort_col:
    filtered_df = filtered_df.sort_values(
        by=sort_col, key=(abs if sort_col == discrepancy_col else None), ascending=False
    )

st.dataframe(filtered_df, use_container_width=True)
st.caption(f"Showing {len(filtered_df)} of {len(audit_df)} flagged transactions.")

# --- Record selection / detail view --------------------------------------
id_col = "Transaction ID" if "Transaction ID" in audit_df.columns else "transaction_id"
if id_col in filtered_df.columns and not filtered_df.empty:
    selected_id = st.selectbox("🔎 Inspect a specific transaction:", filtered_df[id_col].astype(str).tolist())
    if selected_id:
        record = filtered_df[filtered_df[id_col].astype(str) == selected_id].iloc[0]
        with st.expander(f"Full record for {selected_id}", expanded=True):
            st.json(record.dropna().to_dict())

st.markdown("---")

# --- Settlement Q&A AI Assistant ------------------------------------------
st.subheader("💬 Settlement Q&A Assistant")
st.caption("Ask questions about anomalies, contract clauses, or fee structures. "
           "Answers are grounded strictly in the audit data shown above — the assistant is instructed "
           "to say so rather than guess if something isn't in that data.")

api_key = os.environ.get("GROQ_API_KEY")
try:
    client = Groq(api_key=api_key) if api_key else None
except Exception:
    client = None

if "messages" not in st.session_state:
    st.session_state.messages = []

for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])

if prompt := st.chat_input("E.g., 'Why was transaction 1179 escalated?'"):
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    with st.chat_message("assistant"):
        if client:
            # Question-aware context: always includes any transaction ID the
            # user explicitly mentions (not just a positional head() slice),
            # plus the highest-risk rows for general questions.
            context_df = build_qna_context(audit_df, prompt, id_col=id_col)
            slim_records = context_df.drop(columns=["Date", "Narration"], errors="ignore").to_dict(orient="records")
            kpi_summary = compute_kpis(audit_df)

            system_instruction = f"""You are the LedgerLens Financial Q&A Assistant.

Contract: {json.dumps(contract)}
Summary metrics (computed deterministically, not by you): {json.dumps(kpi_summary)}
Audit data provided to you (a subset of the full audit sheet, selected because it is either \
highest-risk or explicitly mentioned in the user's question): {json.dumps(slim_records, default=str)}

Rules:
1. Answer ONLY using the contract terms and audit data given above. Never invent transaction IDs, \
amounts, dates, or root causes that are not present in this data.
2. If the user asks about a transaction ID that does not appear in the data provided, say explicitly \
that you don't have that transaction in the current context rather than guessing or estimating.
3. Cite exact Transaction IDs and show the fee/tax/net math when relevant.
4. Be concise and professional."""

            try:
                messages_payload = [{"role": "system", "content": system_instruction}]
                for m in st.session_state.messages:
                    messages_payload.append({"role": m["role"], "content": m["content"]})

                response = client.chat.completions.create(
                    model="openai/gpt-oss-120b",
                    messages=messages_payload,
                    temperature=0.1,
                )
                answer = response.choices[0].message.content
                st.markdown(answer)
                st.session_state.messages.append({"role": "assistant", "content": answer})
            except Exception as e:
                err_text = f"Error generating response: {e}"
                st.markdown(err_text)
                st.session_state.messages.append({"role": "assistant", "content": err_text})
        else:
            st.markdown("⚠️ Groq client not initialized. Set `GROQ_API_KEY` in your `.env` file "
                         "(copy `api.env.example` to `.env`) and restart the app.")

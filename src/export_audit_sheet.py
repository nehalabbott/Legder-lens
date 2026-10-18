import pandas as pd
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def generate_audit_spreadsheet():
    ledger = pd.read_csv(ROOT / "data" / "ochicken_ledger.csv")
    truth = pd.read_csv(ROOT / "data" / "ochicken_ground_truth.csv")
    exceptions = pd.read_csv(ROOT / "output" / "tier1_exceptions.csv")
    resolutions = pd.read_csv(ROOT / "output" / "agent_resolutions.csv")

    # 1. Merge Tier 1 exception metadata with Tier 2 agent diagnoses. This is
    #    the authoritative row set: every row the reconciler flagged, exactly
    #    once per (transaction_id, exception_type) pair.
    combined = pd.merge(exceptions, resolutions, on="transaction_id", how="left")

    # 2. Enrich with ledger detail. Deduplicate the ledger by txn_id first and
    #    merge FROM combined (not from ledger), so:
    #      a) a transaction that is duplicated in the raw ledger doesn't fan
    #         out into two identical audit rows (bug: previously merging
    #         ledger -> combined caused a DUPLICATE_IN_LEDGER row to appear
    #         twice, double-counting it in every downstream total), and
    #      b) a MISSING_IN_BANK exception -- by definition, a transaction_id
    #         that exists in the gateway report but NOT in the bank ledger --
    #         still makes it into the audit sheet instead of being silently
    #         dropped by an inner join against the ledger.
    ledger_dedup = ledger.drop_duplicates(subset=["txn_id"], keep="first")
    merged = pd.merge(
        combined,
        ledger_dedup[["txn_id", "value_date", "narration", "deposit_amt"]],
        left_on="transaction_id",
        right_on="txn_id",
        how="left",
    )

    # 3. Merge ground truth validation data (also left, for the same reason).
    merged = pd.merge(
        merged,
        truth[["txn_id", "gross_amount", "correct_net", "discrepancy_amount", "injected_error_type"]],
        left_on="transaction_id",
        right_on="txn_id",
        how="left",
        suffixes=("", "_truth"),
    )

    # 4. Select and format audit sheet columns. Transactions missing from one
    #    side (MISSING_IN_BANK / MISSING_IN_GATEWAY) legitimately have gaps
    #    here -- surface that honestly instead of hiding or fabricating it.
    audit_df = pd.DataFrame({
        "Transaction ID": merged["transaction_id"],
        "Date": merged["value_date"].fillna("NOT IN LEDGER"),
        "Narration": merged["narration"].fillna("NOT IN LEDGER"),
        "Gross Amount (₹)": merged["gross_amount"],
        "Stated Bank Net (₹)": merged["deposit_amt"],
        "Correct Expected Net (₹)": merged["correct_net"],
        "Discrepancy (₹)": merged["discrepancy_amount"].fillna(0.0),
        "Detection Rule": merged["exception_type"],
        "Agent Diagnosis": merged["root_cause"],
        "Agent Recommended Action": merged["recommended_action"],
        "Agent Confidence": merged["confidence"],
        "Human Approval Needed": merged["human_approval_required"],
        "Used LLM": merged["confidence"] > 0.0,
        "Actual Planted Error": merged["injected_error_type"].fillna("None (Valid Variance)"),
    })

    audit_df.sort_values(
        by=["Human Approval Needed", "Discrepancy (₹)"],
        key=lambda col: col.abs() if col.name == "Discrepancy (₹)" else col,
        ascending=[False, False],
        inplace=True,
    )

    output_path = ROOT / "output" / "reconciliation_audit_sheet.csv"
    audit_df.to_csv(output_path, index=False)

    print(f"Spreadsheet generated: {output_path}")
    print(f"Total flagged transactions: {len(audit_df)}")

    escalations = audit_df["Agent Recommended Action"].astype(str).str.contains("ESCALATE", case=False).sum()
    ignores = audit_df["Agent Recommended Action"].astype(str).str.contains("IGNORE", case=False).sum()

    print(f"Items flagged for recovery (ESCALATE): {escalations}")
    print(f"Legitimate variances cleared (IGNORE): {ignores}")

    missing_from_ledger = (audit_df["Date"] == "NOT IN LEDGER").sum()
    if missing_from_ledger:
        print(f"⚠️  {missing_from_ledger} transaction(s) exist only in the gateway report "
              f"(MISSING_IN_BANK) and have no ledger date/narration to show.")


if __name__ == "__main__":
    generate_audit_spreadsheet()

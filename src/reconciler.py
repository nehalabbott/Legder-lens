"""
Tier 1: Deterministic reconciliation engine.

Design goal: every financial calculation here is plain arithmetic driven by
`contracts/ochicken_contract.json`. Nothing in this file calls an LLM or
makes a network request, so it is fast, fully deterministic, and cheap to
unit test (see tests/test_reconciler.py).

The module is split into:
  - pure functions (no file I/O) that do the actual reconciliation logic and
    are what the unit tests exercise directly, and
  - run_tier1_reconciliation(), a thin I/O wrapper that loads the CSVs/JSON,
    calls the pure functions, and writes the report.
"""
import pandas as pd
import json
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

PROMO_START = date(2026, 7, 15)
PROMO_END = date(2026, 7, 31)
PROMO_MDR_RATE = 1.50 / 100

NET_TOLERANCE_INR = 0.05  # 5-paise rounding tolerance

REQUIRED_LEDGER_COLUMNS = {"txn_id", "value_date", "txn_type", "narration", "deposit_amt"}
REQUIRED_GATEWAY_COLUMNS = {"transaction_id", "amount", "rzp_fee", "rzp_tax", "rzp_net"}


class ReconciliationInputError(ValueError):
    """Raised when input data is missing required columns or is otherwise unusable."""


def load_contract(contract_path: Path = None) -> dict:
    contract_path = contract_path or (ROOT / "contracts" / "ochicken_contract.json")
    with open(contract_path, "r") as f:
        return json.load(f)


def parse_txn_date(date_str):
    """Robustly parses date strings across multiple common formats. Returns None if unparseable."""
    if pd.isna(date_str):
        return None
    date_str = str(date_str).strip()
    for fmt in ("%d/%m/%y", "%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(date_str, fmt).date()
        except ValueError:
            pass
    return None


def is_upi_routed(narration) -> bool:
    """Matches UPI batch narrations regardless of separator/casing variation."""
    narration_clean = str(narration).upper().replace("_", "-").replace(" ", "")
    return "UPI-BATCH" in narration_clean or "UPIBATCH" in narration_clean


def validate_columns(df: pd.DataFrame, required: set, source_name: str):
    missing = required - set(df.columns)
    if missing:
        raise ReconciliationInputError(
            f"{source_name} is missing required column(s): {sorted(missing)}. "
            f"Found columns: {list(df.columns)}"
        )


def compute_expected_fee_and_net(gross: float, txn_type: str, txn_date, bank_narration: str, contract: dict):
    """
    Pure function: given a gross amount, the transaction type, its date, and
    the bank narration (used to detect UPI routing), returns the
    contractually-expected (fee, tax, net) as a tuple of floats.
    """
    card_rules = contract.get("card_terminal_settlement", {})
    payout_rules = contract.get("razorpay_payout_batch", {})

    if txn_type == "CARD_TERMINAL_SETTLEMENT":
        if txn_date and PROMO_START <= txn_date <= PROMO_END:
            expected_rate = PROMO_MDR_RATE
        else:
            expected_rate = card_rules.get("mdr_rate_percent", 1.75) / 100.0
        expected_fee = round(gross * expected_rate, 2)
        expected_tax = round(expected_fee * (card_rules.get("gst_on_mdr_percent", 18.0) / 100.0), 2)
    else:
        waived_below = payout_rules.get("commission_waived_below_amount", 1000.0)
        if is_upi_routed(bank_narration) or gross < waived_below:
            expected_fee = 0.0
            expected_tax = 0.0
        else:
            expected_rate = payout_rules.get("commission_rate_percent", 2.0) / 100.0
            expected_fee = round(gross * expected_rate, 2)
            expected_tax = round(expected_fee * (payout_rules.get("gst_on_commission_percent", 18.0) / 100.0), 2)

    expected_net = round(gross - expected_fee - expected_tax, 2)
    return expected_fee, expected_tax, expected_net


def find_duplicate_ids(df: pd.DataFrame, id_col: str, exception_type: str, detail: str):
    """Returns one exception dict per duplicated id (not one per duplicate row)."""
    dup = df[df.duplicated(subset=[id_col], keep=False)]
    return [
        {"transaction_id": tx_id, "exception_type": exception_type, "details": detail}
        for tx_id in dup[id_col].unique()
    ]


def categorize_exceptions(settlements: pd.DataFrame, rzp_report: pd.DataFrame, contract: dict):
    """
    Pure reconciliation logic, with no file I/O. `settlements` must already be
    the ledger subset filtered to settlement-bearing transaction types, with
    columns renamed to {transaction_id, bank_net, bank_narration, txn_type,
    value_date}. `rzp_report` is the raw gateway settlement report.

    Returns a list of exception dicts: {transaction_id, exception_type, details}.
    """
    exceptions = []

    # 1. Duplicate detection, independent of the merge below so a duplicate
    #    doesn't also get silently collapsed by drop_duplicates first.
    exceptions += find_duplicate_ids(
        settlements, "transaction_id", "DUPLICATE_IN_LEDGER",
        "Transaction ID appears multiple times in bank ledger",
    )
    exceptions += find_duplicate_ids(
        rzp_report, "transaction_id", "DUPLICATE_IN_GATEWAY",
        "Transaction ID appears multiple times in gateway settlement report",
    )

    # Dedup for the outer-join comparison below; the duplicate itself was
    # already captured as its own exception above.
    clean_settlements = settlements.drop_duplicates(subset=["transaction_id"], keep="first")
    clean_rzp = rzp_report.drop_duplicates(subset=["transaction_id"], keep="first")

    recon = pd.merge(clean_settlements, clean_rzp, on="transaction_id", how="outer")

    for _, row in recon.iterrows():
        tx_id = row["transaction_id"]

        if pd.isna(row.get("amount")):
            exceptions.append({
                "transaction_id": tx_id,
                "exception_type": "MISSING_IN_GATEWAY",
                "details": f"Bank net: {row.get('bank_net', 0.0)}",
            })
            continue

        if pd.isna(row.get("bank_net")):
            exceptions.append({
                "transaction_id": tx_id,
                "exception_type": "MISSING_IN_BANK",
                "details": f"Gateway net: {row.get('rzp_net', 0.0)}",
            })
            continue

        gross = row["amount"]
        if pd.isna(gross) or gross <= 0:
            exceptions.append({
                "transaction_id": tx_id,
                "exception_type": "INVALID_DATA",
                "details": f"Non-positive or missing gross amount: {gross}",
            })
            continue

        gross = float(gross)
        txn_type = row.get("txn_type")
        bank_net = float(row["bank_net"])
        rzp_net = float(row.get("rzp_net", 0.0))
        rzp_fee = float(row.get("rzp_fee", 0.0))
        rzp_tax = float(row.get("rzp_tax", 0.0))

        txn_date = parse_txn_date(row.get("value_date")) if txn_type == "CARD_TERMINAL_SETTLEMENT" else None
        expected_fee, expected_tax, expected_net = compute_expected_fee_and_net(
            gross, txn_type, txn_date, row.get("bank_narration", ""), contract
        )

        # Checked in order of financial severity: a wrong net is worse than a
        # wrong fee component, so the first failing check wins and the rest
        # are skipped for that row (they'd likely be downstream symptoms).
        if abs(bank_net - rzp_net) > NET_TOLERANCE_INR:
            exceptions.append({
                "transaction_id": tx_id,
                "exception_type": "NET_MISMATCH",
                "details": f"Bank received {bank_net}, Gateway reported {rzp_net}",
            })
        elif abs(bank_net - expected_net) > NET_TOLERANCE_INR:
            exceptions.append({
                "transaction_id": tx_id,
                "exception_type": "CONTRACT_VARIANCE",
                "details": f"Bank received {bank_net}, but contract terms dictate {expected_net}",
            })
        elif abs(rzp_fee - expected_fee) > NET_TOLERANCE_INR:
            exceptions.append({
                "transaction_id": tx_id,
                "exception_type": "FEE_VARIANCE",
                "details": f"Charged fee {rzp_fee} differs from expected fee {expected_fee}",
            })
        elif abs(rzp_tax - expected_tax) > NET_TOLERANCE_INR:
            exceptions.append({
                "transaction_id": tx_id,
                "exception_type": "TAX_VARIANCE",
                "details": f"Deducted tax {rzp_tax} differs from expected GST {expected_tax}",
            })

    return exceptions


def run_tier1_reconciliation():
    contract = load_contract()

    ledger = pd.read_csv(ROOT / "data" / "ochicken_ledger.csv")
    rzp_report = pd.read_csv(ROOT / "data" / "razorpay_settlement_report.csv")

    validate_columns(ledger, REQUIRED_LEDGER_COLUMNS, "data/ochicken_ledger.csv")
    validate_columns(rzp_report, REQUIRED_GATEWAY_COLUMNS, "data/razorpay_settlement_report.csv")

    settlements = ledger[ledger["txn_type"].isin(["CARD_TERMINAL_SETTLEMENT", "RAZORPAY_PAYOUT_BATCH"])].copy()
    settlements = settlements.rename(columns={
        "txn_id": "transaction_id", "deposit_amt": "bank_net", "narration": "bank_narration",
    })

    exceptions = categorize_exceptions(settlements, rzp_report, contract)

    output_dir = ROOT / "output"
    output_dir.mkdir(exist_ok=True)
    pd.DataFrame(exceptions, columns=["transaction_id", "exception_type", "details"]).to_csv(
        output_dir / "tier1_exceptions.csv", index=False
    )

    unique_exception_ids = {e["transaction_id"] for e in exceptions}
    clean_count = len(set(settlements["transaction_id"]) - unique_exception_ids)
    match_rate = (clean_count / len(settlements)) * 100 if len(settlements) else 0

    print("Tier 1 Deterministic Engine Complete.")
    print(f"Total Settlements Analyzed: {len(settlements)}")
    print(f"Exceptions Found: {len(exceptions)} ({len(unique_exception_ids)} unique transaction IDs)")
    print(f"Match Rate: {match_rate:.2f}%")
    print("Report written to output/tier1_exceptions.csv")


if __name__ == "__main__":
    run_tier1_reconciliation()

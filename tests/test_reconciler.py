"""
Unit tests for the Tier 1 deterministic reconciliation engine.

These tests build small, hand-crafted DataFrames in memory rather than
relying on the stochastic synthetic generator (src/generate_data.py), so
every code path -- including ones the random generator rarely or never
produces, like MISSING_IN_BANK, FEE_VARIANCE, and TAX_VARIANCE -- gets
direct, deterministic coverage.

Run with: pytest tests/test_reconciler.py -v
"""
from datetime import date

import pandas as pd
import pytest

from src.reconciler import (
    ReconciliationInputError,
    categorize_exceptions,
    compute_expected_fee_and_net,
    is_upi_routed,
    parse_txn_date,
    validate_columns,
)

CONTRACT = {
    "card_terminal_settlement": {"mdr_rate_percent": 1.8, "gst_on_mdr_percent": 18},
    "razorpay_payout_batch": {
        "commission_rate_percent": 2,
        "gst_on_commission_percent": 18,
        "commission_waived_below_amount": 1000,
    },
}


def make_settlement_row(tx_id, bank_net, txn_type="CARD_TERMINAL_SETTLEMENT",
                         narration="TERMINAL 1 CARDS SETTL.", value_date="05/08/26"):
    return {
        "transaction_id": tx_id,
        "value_date": value_date,
        "txn_type": txn_type,
        "bank_narration": narration,
        "bank_net": bank_net,
    }


def make_gateway_row(tx_id, amount, fee, tax, net):
    return {"transaction_id": tx_id, "amount": amount, "rzp_fee": fee, "rzp_tax": tax, "rzp_net": net}


# ---------------------------------------------------------------------------
# parse_txn_date
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("20/07/26", date(2026, 7, 20)),
    ("20/07/2026", date(2026, 7, 20)),
    ("2026-07-20", date(2026, 7, 20)),
    ("20-07-2026", date(2026, 7, 20)),
])
def test_parse_txn_date_accepts_common_formats(raw, expected):
    assert parse_txn_date(raw) == expected


def test_parse_txn_date_returns_none_for_garbage():
    assert parse_txn_date("not-a-date") is None
    assert parse_txn_date(None) is None
    assert parse_txn_date(float("nan")) is None


# ---------------------------------------------------------------------------
# is_upi_routed
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("narration,expected", [
    ("RAZORPAY PAYOUT UPI-BATCH 20/07/26", True),
    ("RAZORPAY PAYOUT UPI_BATCH 20/07/26", True),
    ("razorpay payout upi batch 20/07/26", True),
    ("RAZORPAY PAYOUT CARD-ONLINE-BATCH 20/07/26", False),
    ("", False),
])
def test_is_upi_routed(narration, expected):
    assert is_upi_routed(narration) is expected


# ---------------------------------------------------------------------------
# compute_expected_fee_and_net -- the actual money math
# ---------------------------------------------------------------------------

def test_card_settlement_standard_rate():
    fee, tax, net = compute_expected_fee_and_net(10000.0, "CARD_TERMINAL_SETTLEMENT", date(2026, 8, 5), "", CONTRACT)
    assert fee == 180.0          # 1.8% of 10000
    assert tax == 32.4           # 18% of 180
    assert net == 9787.6


def test_card_settlement_promo_window_boundaries():
    # Promo is inclusive of both start and end dates.
    for d in (date(2026, 7, 15), date(2026, 7, 31)):
        fee, _, _ = compute_expected_fee_and_net(10000.0, "CARD_TERMINAL_SETTLEMENT", d, "", CONTRACT)
        assert fee == 150.0  # 1.5% promo rate

    for d in (date(2026, 7, 14), date(2026, 8, 1)):
        fee, _, _ = compute_expected_fee_and_net(10000.0, "CARD_TERMINAL_SETTLEMENT", d, "", CONTRACT)
        assert fee == 180.0  # back to standard rate just outside the window


def test_payout_commission_waived_below_threshold():
    # Regression test: the shipped contract.json once set this threshold to
    # 100 instead of 1000, silently disabling the exemption the synthetic
    # "boundary" test data was designed to exercise.
    fee, tax, net = compute_expected_fee_and_net(999.99, "RAZORPAY_PAYOUT_BATCH", None, "CARD-ONLINE-BATCH", CONTRACT)
    assert fee == 0.0 and tax == 0.0 and net == 999.99

    fee, tax, net = compute_expected_fee_and_net(1000.00, "RAZORPAY_PAYOUT_BATCH", None, "CARD-ONLINE-BATCH", CONTRACT)
    assert fee == 20.0  # at-or-above threshold, commission applies
    assert net == 976.4


def test_payout_commission_waived_for_upi_regardless_of_amount():
    fee, tax, net = compute_expected_fee_and_net(50000.0, "RAZORPAY_PAYOUT_BATCH", None, "RAZORPAY PAYOUT UPI-BATCH", CONTRACT)
    assert fee == 0.0 and tax == 0.0 and net == 50000.0


def test_payout_commission_charged_for_card_online_above_threshold():
    fee, tax, net = compute_expected_fee_and_net(50000.0, "RAZORPAY_PAYOUT_BATCH", None, "CARD-ONLINE-BATCH", CONTRACT)
    assert fee == 1000.0    # 2% of 50000
    assert tax == 180.0     # 18% of 1000
    assert net == 48820.0


# ---------------------------------------------------------------------------
# categorize_exceptions -- the end-to-end matching logic
# ---------------------------------------------------------------------------

def test_clean_matching_transaction_produces_no_exception():
    # gross 10000, standard card rate => fee 180, tax 32.4, net 9787.6
    settlements = pd.DataFrame([make_settlement_row("T1", 9787.6)])
    gateway = pd.DataFrame([make_gateway_row("T1", 10000.0, 180.0, 32.4, 9787.6)])
    assert categorize_exceptions(settlements, gateway, CONTRACT) == []


def test_net_mismatch_detected():
    settlements = pd.DataFrame([make_settlement_row("T1", 9000.0)])  # bank says 9000
    gateway = pd.DataFrame([make_gateway_row("T1", 10000.0, 180.0, 32.4, 9787.6)])  # gateway says 9787.6
    exc = categorize_exceptions(settlements, gateway, CONTRACT)
    assert len(exc) == 1
    assert exc[0]["exception_type"] == "NET_MISMATCH"


def test_net_mismatch_within_tolerance_is_not_flagged():
    settlements = pd.DataFrame([make_settlement_row("T1", 9787.64)])  # 4 paise off
    gateway = pd.DataFrame([make_gateway_row("T1", 10000.0, 180.0, 32.4, 9787.6)])
    assert categorize_exceptions(settlements, gateway, CONTRACT) == []


def test_net_mismatch_just_outside_tolerance_is_flagged():
    settlements = pd.DataFrame([make_settlement_row("T1", 9787.66)])  # 6 paise off
    gateway = pd.DataFrame([make_gateway_row("T1", 10000.0, 180.0, 32.4, 9787.6)])
    exc = categorize_exceptions(settlements, gateway, CONTRACT)
    assert len(exc) == 1 and exc[0]["exception_type"] == "NET_MISMATCH"


def test_contract_variance_when_both_sides_agree_but_disobey_contract():
    # Bank and gateway collude on a wrong net (e.g. gateway misapplied the
    # promo rate outside the promo window); NET_MISMATCH won't catch this
    # since both sides agree with each other.
    wrong_net = 9850.0  # computed with a rate far below the 1.8% standard
    settlements = pd.DataFrame([make_settlement_row("T1", wrong_net, value_date="05/08/26")])
    gateway = pd.DataFrame([make_gateway_row("T1", 10000.0, 123.0, 27.0, wrong_net)])
    exc = categorize_exceptions(settlements, gateway, CONTRACT)
    assert len(exc) == 1
    assert exc[0]["exception_type"] == "CONTRACT_VARIANCE"


def test_fee_variance_when_net_correct_but_fee_tax_split_wrong():
    # Net matches the correct value, but the gateway merged tax into the fee
    # field instead of reporting it separately.
    gross, fee, tax = 10000.0, 180.0, 32.4
    net = round(gross - fee - tax, 2)
    settlements = pd.DataFrame([make_settlement_row("T1", net, value_date="05/08/26")])
    gateway = pd.DataFrame([make_gateway_row("T1", gross, fee + tax, 0.0, net)])
    exc = categorize_exceptions(settlements, gateway, CONTRACT)
    assert len(exc) == 1
    assert exc[0]["exception_type"] == "FEE_VARIANCE"


def test_missing_in_gateway():
    settlements = pd.DataFrame([make_settlement_row("T1", 9787.6)])
    gateway = pd.DataFrame(columns=["transaction_id", "amount", "rzp_fee", "rzp_tax", "rzp_net"])
    exc = categorize_exceptions(settlements, gateway, CONTRACT)
    assert len(exc) == 1 and exc[0]["exception_type"] == "MISSING_IN_GATEWAY"


def test_missing_in_bank():
    settlements = pd.DataFrame(columns=["transaction_id", "value_date", "txn_type", "bank_narration", "bank_net"])
    gateway = pd.DataFrame([make_gateway_row("T1", 10000.0, 180.0, 32.4, 9787.6)])
    exc = categorize_exceptions(settlements, gateway, CONTRACT)
    assert len(exc) == 1 and exc[0]["exception_type"] == "MISSING_IN_BANK"


def test_duplicate_in_ledger_detected_once_per_id_not_per_row():
    settlements = pd.DataFrame([
        make_settlement_row("T1", 9787.6),
        make_settlement_row("T1", 9787.6),
        make_settlement_row("T1", 9787.6),
    ])
    gateway = pd.DataFrame([make_gateway_row("T1", 10000.0, 180.0, 32.4, 9787.6)])
    exc = categorize_exceptions(settlements, gateway, CONTRACT)
    dup_exceptions = [e for e in exc if e["exception_type"] == "DUPLICATE_IN_LEDGER"]
    assert len(dup_exceptions) == 1  # one exception for the id, not three


def test_duplicate_in_gateway_detected():
    settlements = pd.DataFrame([make_settlement_row("T1", 9787.6)])
    gateway = pd.DataFrame([
        make_gateway_row("T1", 10000.0, 180.0, 32.4, 9787.6),
        make_gateway_row("T1", 10000.0, 180.0, 32.4, 9787.6),
    ])
    exc = categorize_exceptions(settlements, gateway, CONTRACT)
    assert any(e["exception_type"] == "DUPLICATE_IN_GATEWAY" for e in exc)


def test_invalid_data_negative_or_zero_gross_is_flagged_not_crashed():
    settlements = pd.DataFrame([make_settlement_row("T1", 100.0)])
    gateway = pd.DataFrame([make_gateway_row("T1", -50.0, 0.0, 0.0, 100.0)])
    exc = categorize_exceptions(settlements, gateway, CONTRACT)
    assert len(exc) == 1 and exc[0]["exception_type"] == "INVALID_DATA"


def test_empty_inputs_produce_no_exceptions_and_do_not_crash():
    empty_settlements = pd.DataFrame(columns=["transaction_id", "value_date", "txn_type", "bank_narration", "bank_net"])
    empty_gateway = pd.DataFrame(columns=["transaction_id", "amount", "rzp_fee", "rzp_tax", "rzp_net"])
    assert categorize_exceptions(empty_settlements, empty_gateway, CONTRACT) == []


# ---------------------------------------------------------------------------
# validate_columns
# ---------------------------------------------------------------------------

def test_validate_columns_raises_clear_error_on_missing_column():
    df = pd.DataFrame({"transaction_id": [1]})
    with pytest.raises(ReconciliationInputError, match="amount"):
        validate_columns(df, {"transaction_id", "amount"}, "test_source.csv")


def test_validate_columns_passes_when_all_present():
    df = pd.DataFrame({"a": [1], "b": [2]})
    validate_columns(df, {"a", "b"}, "test_source.csv")  # should not raise
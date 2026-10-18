## Summary

This document is a running engineering changelog for LedgerLens (originally built for the Razorpay AI
Buildathon 2026 as the "O'Chicken AI Finance Controller"). The first entry below covers the initial
buildathon hardening pass; see "Round 2" further down for a later correctness/portfolio audit.

## Round 1: Buildathon Hardening

This round addressed the Razorpay Buildathon judge evaluation, hardening the AI Finance Controller from a strong prototype into a production-grade operational system. It resolves critical security vulnerabilities, standardizes the AI architecture, and completely decouples the evaluation harness for honest metric reporting.

## 🔒 Security & Webhook Hardening
- **Purged Credentials:** Completely removed plaintext API keys from `api.env.example` and scrubbed history to resolve the submission hygiene deduction.
- **Webhook Authentication:** Implemented `X-Razorpay-Signature` validation via HMAC-SHA256 in `webhook_receiver.py`.
- **Idempotency:** Added an in-memory event store checking `x-razorpay-event-id` to prevent duplicate processing of Razorpay's at-least-once delivery webhooks.
- **Payload Adapter:** Added a dedicated transformation layer to convert genuine Razorpay webhook payloads into the internal normalized schema.

## 🤖 AI Architecture Consolidation
- **Unified Provider:** Purged all Gemini SDK remnants from `qna_agent.py`, `webhook_receiver.py`, and `alerts.py` to eliminate mixed-architecture configuration. The entire repository now strictly uses Groq (`openai/gpt-oss-120b`).
- **Operational JSON Schema:** Upgraded Tier 2 (`agent.py`) from generating unstructured text to enforcing a strict JSON operational schema: `root_cause`, `confidence`, `recommended_action`, and `human_approval_required`.
- **Contract Grounding:** Tier 2 now explicitly reads and injects the merchant contract clauses into the LLM context to ground its diagnostics.

## 📉 Tier 1 & Evaluation Decoupling
- **GST & Duplicate Checks:** `reconciler.py` now verifies gateway tax deductions (`rzp_tax`) and actively flags duplicate transaction IDs before merging.
- **Rounding Tolerances:** Added a 5-paise rounding tolerance to eliminate false positives caused by floating-point discrepancies.
- **Independent Evaluation:** `generate_data.py` was decoupled to natively track gateway-side errors (preventing the false positive penalty on transaction 2136).
- **Stress Testing:** Injected explicit boundary edge cases (₹1,000 threshold, ₹999.99, and exact promo start/end dates) directly into the synthetic data to stress-test Tier 1 logic.

## 📊 UX & Operator Dashboard
- **Operator Triage Card:** Updated the Streamlit dashboard (`app.py`) to move away from a raw engineering view by surfacing the single highest-risk anomaly in an actionable Operator Queue.
- **Live Metric Alerts:** `alerts.py` now dynamically queries the audit sheet for the highest-risk transaction and formats a rich Slack/Teams card.

## ✅ Verification
Running `python main.py` followed by `python tests/eval_harness.py` executes cleanly on the newly generated synthetic batch. The webhook daemon (`uvicorn src.webhook_receiver:app`) now rejects unsigned payloads and duplicate event IDs.

---

## Round 2: Correctness & Portfolio Hardening

A follow-up audit found that Round 1 introduced real functional and documentation bugs alongside its
improvements. This round fixes those and adds proper automated test coverage.

### 🐛 Critical bugs fixed
- **`contracts/ochicken_contract.json`**: `commission_waived_below_amount` was set to `100`, not the
  `1000` every doc, README, and the synthetic "boundary" test transactions (₹999.99 / ₹1000.00)
  assumed. This silently disabled the UPI/low-value commission exemption and meant the one edge case
  the project claimed to stress-test was never actually exercised. Fixed, and now covered by a
  regression test (`tests/test_reconciler.py::test_payout_commission_waived_below_threshold`).
- **`export_audit_sheet.py`**: the final merge joined *from* the raw bank ledger instead of from the
  exceptions table. Two consequences: (1) a `DUPLICATE_IN_LEDGER` transaction fanned out into two
  identical rows in the audit sheet, double-counting it in every downstream total; (2)
  `MISSING_IN_BANK` exceptions — transactions the gateway processed but the bank ledger never
  received — were silently dropped by the inner join, since they by definition don't exist in the
  ledger lookup table. Fixed by merging from the exceptions table with `how="left"`.
- **`requirements.txt` did not exist.** `pip install -r requirements.txt`, as instructed by the
  original README, failed outright for anyone cloning the repo. Added, pinned.
- **Q&A context truncation**: both the Streamlit assistant and (to a lesser extent) the CLI tool built
  their LLM context with a positional slice (`audit_df.head(25)`), so a question about a transaction
  outside that window had no way to be answered correctly. Replaced with `dashboard_utils.build_qna_context`,
  which always includes any transaction ID explicitly mentioned in the question plus the highest-risk
  rows for general questions, and strengthened both system prompts to explicitly refuse to guess about
  transactions not present in the provided context.
- **`.env` was never actually loaded.** `api.env.example` existed but nothing in the codebase read it.
  Added `python-dotenv` + `load_dotenv()` to every entrypoint that needs an API key or secret.
- **Webhook signature verification failed open** (accepted unsigned requests when the secret wasn't
  configured) with no way to change that. Added `WEBHOOK_STRICT_MODE` to fail closed instead, and fixed
  the settlement timestamp to come from the actual Razorpay payload (`created_at`) rather than
  `datetime.now()`, which could misclassify promo-window eligibility for any transaction not processed
  on the same day the webhook fires.

### 🧪 Testing
- `reconciler.py` was refactored into pure, file-I/O-free functions (`compute_expected_fee_and_net`,
  `categorize_exceptions`, …) specifically so they could be unit tested without the stochastic data
  generator. `tests/test_reconciler.py` adds 29 tests covering every exception type the engine can
  produce, including ones the synthetic generator rarely or never hit before this round
  (`MISSING_IN_BANK`, `DUPLICATE_IN_GATEWAY`, `FEE_VARIANCE`, `CONTRACT_VARIANCE`), plus the tolerance
  boundary and the threshold-bug regression above.
- Dashboard KPI math and the Q&A context-selection logic were pulled out of `app.py` into
  `dashboard_utils.py` so they're unit-testable without a running Streamlit session
  (`tests/test_dashboard_utils.py`).
- Added `tests/test_webhook_receiver.py` (FastAPI `TestClient`) covering signature verification,
  idempotency, and strict mode.
- Added a GitHub Actions workflow (`.github/workflows/tests.yml`) that installs dependencies, runs
  `pytest`, and smoke-tests the full deterministic pipeline on every push/PR.

### 📊 Honest metrics
`generate_data.py` was previously generating ~50 transactions over a 22-day window while the README
claimed "1,412+ ledger transactions" across a "30-day window." The generator now genuinely spans 30
days (~75-80 transactions per run, synthetic data is randomized so the exact count varies run to run)
and the README's numbers are pulled from an actual run instead of being aspirational. The eval harness
numbers are also reported in full, including precision (not just recall) and an explicit note on why
duplicate-detection exceptions always register as "false positives" against a ground-truth file that
only tracks financial-amount discrepancies — they're still correct things to flag, just not scored by
this particular harness.

### ⚠️ Known limitations this round did not fix
- `is_upi_routed` is hardcoded to `False` in the live webhook adapter (see the docstring in
  `webhook_receiver.py::adapt_razorpay_payload` for why — the settlement webhook payload doesn't carry
  a payment-method breakdown).
- The fee/net calculation logic now lives in `reconciler.py`, but `webhook_receiver.py` still keeps an
  independent copy (`compute_expected_fee`) rather than importing it, so a future contract change has
  to be made in two places. Low risk today (both are covered by their own tests), but worth
  consolidating into a shared `pricing.py` module next.
- `tests/test_webhook_receiver.py` could not be executed in the sandbox this audit was performed in
  (no network access to install `fastapi`/`httpx`); it was written and reviewed carefully but should be
  run once locally (`pytest tests/test_webhook_receiver.py`) before relying on it in CI.
**Component Pipeline**

**`src/generate_data.py`**
Simulates a representative 30-day settlement cycle (1,400+ ledger entries) featuring realistic banking patterns: card terminal deposits, batch payout batches, high-frequency direct UPI credits, and NEFT vendor debits. Injects realistic edge cases:
* Contractual promotional rate switches and precise boundary testing (e.g., ₹999.99 vs ₹1,000 thresholds).
* Duplicate ledger entries and gateway dropped records (missing settlement entries).
* GST base miscalculations (gross vs fee base).
* Accidental double deductions and commission charged on fee-exempt UPI batches.

**`contracts/ochicken_contract.json`**
Machine-readable configuration containing legal fee clauses:
* `card_terminal_settlement`: Standard MDR (1.75%) + GST (18%).
* `promotional_clause`: 1.5% MDR between July 15, 2026 and July 31, 2026.
* `razorpay_payout_batch`: 2.0% commission; 0% commission on UPI routes and transactions below ₹1,000.

**`src/reconciler.py` (Tier 1)**
Performs deterministic outer joins across data sources. Evaluates mathematical parity (Expected Net = Gross - MDR - GST) with a 5-paise rounding tolerance. Includes robust date parsing, GST verification, and pre-merge duplicate detection. Flags records deviating from base formulas into `output/tier1_exceptions.csv`.

**`src/agent.py` (Tier 2)**
Ingests exception rows alongside contract constraints. Employs a batched Groq payload (`openai/gpt-oss-120b`) to classify root causes. Outputs a strict operational JSON schema:
* `root_cause`: Precise explanation of the discrepancy.
* `confidence`: Float score mapping certainty.
* `recommended_action`: Operational directive (e.g., `IGNORE`, `ESCALATE TO GATEWAY`).
* `human_approval_required`: Boolean trigger for dispute routing.

**`src/webhook_receiver.py` (Tier 3)**
FastAPI real-time gateway daemon. Authenticates incoming Razorpay payloads using HMAC-SHA256 signature validation and enforces event-ID idempotency to prevent duplicate processing. Normalizes the payload and routes it through Tier 1 and Tier 2 logic instantly.

**`src/app.py` (Tier 4)**
Streamlit-powered Finance Operations Hub. Surfaces a prioritized Highest Risk Anomaly queue for instant operator action, tracks overall capital recovery metrics, and provides a token-optimized conversational Q&A assistant to query contract nuances.

**`src/alerts.py`**
Notification dispatcher that parses live evaluation metrics and the highest-risk anomaly directly from the audit sheet, formatting a rich-markdown summary block for Slack/Teams integration.

**`src/export_audit_sheet.py`**
Synthesizes inputs into `output/reconciliation_audit_sheet.csv`. Maps the strict agent JSON schema into human-readable columns and detects deterministic fallbacks based on 0.0 confidence scores.

**`tests/eval_harness.py`**
Compares agent decisions against hidden ground-truth labels (`ochicken_ground_truth.csv`) to compute objective metrics. Evaluates against the full ground truth array to prevent inflated recall, and accurately handles gateway-side anomalies to eliminate artificial false positive penalties.
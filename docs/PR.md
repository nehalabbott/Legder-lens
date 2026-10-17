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
# LedgerLens

**A multi-tier financial reconciliation engine that matches bank settlement ledgers against payment
gateway reports, flags discrepancies deterministically, and uses an LLM only to triage and explain —
never to calculate.**

Originally built for the Razorpay AI Buildathon 2026 (as the "O'Chicken AI Finance Controller") and
since hardened with a full correctness and security audit: a broken commission-threshold bug, a
data-dropping/double-counting export bug, missing dependency pinning, a context-truncation bug in the
Q&A assistant, and 29 new unit tests were all added/fixed as part of that audit. See
[`docs/PR.md`](docs/PR.md) for the full changelog of both hardening passes.

---

## Why this exists

Finance teams at any company that takes payments through a gateway (Razorpay, Stripe, etc.) have to
answer one unglamorous but expensive question every settlement cycle: **did the money that landed in
the bank account actually match what the gateway said it would send, after fees and tax?** Doing this
by hand in a spreadsheet doesn't scale past a few dozen transactions, and it's exactly the kind of
task where "paste everything into an LLM and ask it to find problems" is the wrong tool — LLMs are
unreliable at arithmetic at scale and will confidently miss or invent discrepancies. LedgerLens splits
the problem in two: **deterministic Python does every financial calculation**, and the LLM is only
ever asked to look at a *pre-computed* list of exceptions and explain them in plain English.

---

## System architecture

```
┌─────────────────┐     ┌──────────────────┐     ┌───────────────────┐
│  Bank Ledger CSV │     │ Gateway Report CSV│     │  Merchant Contract │
│ (ochicken_ledger)│     │(razorpay_settle.) │     │   (JSON rules)     │
└────────┬─────────┘     └────────┬──────────┘     └─────────┬──────────┘
         └───────────────┬────────┘                          │
                          ▼                                  │
              ┌───────────────────────┐                      │
              │  Tier 1: Deterministic │◄─────────────────────┘
              │  Reconciler            │   src/reconciler.py
              │  (pure Python/pandas)  │
              └───────────┬────────────┘
                          ▼  exceptions only
              ┌───────────────────────┐
              │  Tier 2: AI Diagnostic │   src/agent.py
              │  Agent (Groq LLM)      │   explains WHY, never WHAT
              └───────────┬────────────┘
                          ▼
              ┌───────────────────────┐      ┌───────────────────────┐
              │  Audit Sheet Export    │─────►│  Tier 4: Streamlit     │
              │  src/export_audit_sheet│      │  Dashboard + Q&A       │
              └────────────────────────┘      │  src/app.py            │
                                               └───────────────────────┘
┌──────────────────────────┐
│ Tier 3: Live Webhook      │  src/webhook_receiver.py
│ Gateway (FastAPI)         │  real-time settlement.processed events
└──────────────────────────┘
```

* **Tier 1 — Deterministic engine (`src/reconciler.py`).** Pure pandas/Python. Cross-references the
  bank ledger against the gateway settlement report, computes the contractually-expected fee/tax/net
  for every transaction (including the promotional MDR window and the UPI/below-threshold commission
  exemption), and classifies every mismatch into one of eight exception types: `NET_MISMATCH`,
  `CONTRACT_VARIANCE`, `FEE_VARIANCE`, `TAX_VARIANCE`, `MISSING_IN_GATEWAY`, `MISSING_IN_BANK`,
  `DUPLICATE_IN_LEDGER`, `DUPLICATE_IN_GATEWAY`, and `INVALID_DATA`. Nothing in this file calls an LLM.
* **Tier 2 — AI diagnostic agent (`src/agent.py`).** Takes *only* the exceptions Tier 1 already found
  and asks Groq (`openai/gpt-oss-120b`) to return a strict JSON schema: `root_cause`, `confidence`,
  `recommended_action`, `human_approval_required`. If no `GROQ_API_KEY` is set, every exception
  deterministically falls back to a generic placeholder diagnosis with `confidence=0.0` and
  `human_approval_required=True` — the pipeline still runs and still produces a usable audit sheet, it
  just honestly flags that no AI judgment was involved.
* **Tier 3 — live webhook gateway (`src/webhook_receiver.py`).** A FastAPI service that reconciles a
  single settlement in real time as Razorpay's `settlement.processed` webhook fires, with HMAC-SHA256
  signature verification and event-ID idempotency.
* **Tier 4 — dashboard & Q&A (`src/app.py`).** A Streamlit app: a KPI summary, a highest-risk operator
  action queue, a filterable/sortable audit table with per-transaction detail view, and a chat
  assistant that answers questions about the audit data and contract — grounded strictly in the data
  actually shown to it (see "AI workflow" below).

---

## The reconciliation algorithm

For every settlement transaction, Tier 1:

1. **Matches by transaction ID** across the bank ledger and gateway report (an outer join, so rows
   present on only one side are preserved, not silently dropped).
2. **Flags duplicates independently** — one exception per duplicated ID, not one per duplicate row, on
   each side before any matching happens.
3. For matched rows, **computes the expected fee, tax, and net** from the machine-readable contract
   (`contracts/ochicken_contract.json`):
   - Card settlements: `fee = gross × mdr_rate` (1.5% during the July 15–31 promo window, 1.8%
     otherwise), `tax = fee × 18%`.
   - Payout batches: `fee = gross × 2%`, `tax = fee × 18%` — **waived entirely** if the batch is
     UPI-routed (detected from the bank narration) or the gross amount is below ₹1,000.
4. **Compares in order of financial severity**, so the first real problem found short-circuits the
   rest (a wrong net overall is reported as `NET_MISMATCH` rather than also reporting a `FEE_VARIANCE`
   that's really just a symptom of it):
   - `bank_net` vs `gateway_net` → `NET_MISMATCH` if they disagree with each other.
   - `bank_net` vs `expected_net` → `CONTRACT_VARIANCE` if both sides agree with each other but not
     with the contract (e.g. the gateway silently applied the wrong rate and the bank just paid out
     whatever it was told).
   - `gateway_fee` vs `expected_fee` → `FEE_VARIANCE` (net is right, but the fee/tax split is wrong —
     e.g. GST merged into the fee line instead of reported separately).
   - `gateway_tax` vs `expected_tax` → `TAX_VARIANCE`.
   - All comparisons use a 5-paise tolerance to absorb normal floating-point rounding, not real
     discrepancies.

All of the above is exercised directly by `tests/test_reconciler.py` using hand-built DataFrames, not
just by whatever the random synthetic generator happens to produce on a given run.

---

## AI workflow (and how it's kept honest)

The LLM is used in exactly two places, and both are deliberately constrained:

1. **Tier 2 diagnosis (`src/agent.py`)** receives *only* the already-computed exception rows (type,
   amounts, dates) plus the contract text — never the full transaction dataset — and is asked for a
   structured JSON diagnosis, not prose. If the API key is missing or a call fails, that row gets a
   clearly-labeled fallback (`confidence: 0.0`), not a hallucinated-looking fake diagnosis.
2. **The Q&A assistant (`src/app.py`, `src/qna_agent.py`)** answers questions about the audit data and
   contract. Its context is built by `src/dashboard_utils.py::build_qna_context`, which — instead of a
   naive `.head(25)` slice — always includes any transaction ID the user explicitly mentions, plus the
   highest-risk rows for general questions. Its system prompt explicitly instructs it to say "I don't
   have that transaction in the current context" rather than guess, and never to invent transaction
   IDs, amounts, or dates not present in the data it was given. This is unit tested in
   `tests/test_dashboard_utils.py`.

**What the AI does not do:** decide whether a transaction is a discrepancy, compute any fee/tax/net
figure, or decide the final dollar amount flagged as "at risk." That is 100% deterministic arithmetic
in `reconciler.py`, independent of whether an LLM is configured at all.

---

## Quick start

```bash
git clone https://github.com/nehalabbott/ai-finance-controller.git
cd ai-finance-controller
pip install -r requirements.txt
cp api.env.example .env          # then edit .env with a real GROQ_API_KEY (optional, see below)
python main.py                   # runs the full deterministic + AI pipeline, ~5-10s
python -m streamlit run src/app.py   # launches the dashboard at http://localhost:8501
```

`GROQ_API_KEY` is **optional**. Without it, `python main.py` still runs the entire pipeline end to end
and produces a complete, correct audit sheet — Tier 2 just uses its deterministic fallback instead of
a live LLM diagnosis, and says so explicitly in the console output and in the dashboard.

### Running on Windows (PowerShell)

```powershell
git clone https://github.com/nehalabbott/ai-finance-controller.git
cd ai-finance-controller
pip install -r requirements.txt
copy api.env.example .env
# edit .env with a text editor
python main.py
python -m streamlit run src/app.py
```

### Running the test suite

```bash
pytest tests/ -v
```

29 of the test suite's unit tests (`test_reconciler.py`, `test_dashboard_utils.py`) depend only on
`pandas` and run in well under a second. `test_webhook_receiver.py` additionally needs `fastapi` +
`httpx` (both in `requirements.txt`). A GitHub Actions workflow
(`.github/workflows/tests.yml`) runs the full suite plus an end-to-end pipeline smoke test on every
push.

### Running the live webhook gateway

```bash
python -m uvicorn src.webhook_receiver:app --reload --port 8000
# interactive docs at http://localhost:8000/docs
```

By default, if `RAZORPAY_WEBHOOK_SECRET` isn't set, the gateway logs a loud warning and accepts
unsigned requests (useful for local testing against the FastAPI docs UI). Set
`WEBHOOK_STRICT_MODE=true` in `.env` to make it refuse unsigned requests instead — do this before
pointing it at anything resembling a real endpoint.

### Running with Docker Compose

```bash
docker-compose up --build
```

---

## Sample data & expected output

`src/generate_data.py` produces a synthetic but structurally realistic 30-day settlement cycle: card
terminal settlements, Razorpay payout batches (UPI and card-online routed), and a fixed set of
deliberately injected edge cases — a transaction right at the ₹1,000 commission-exemption threshold,
one just below it, promo-window boundary dates, a duplicated ledger entry, a duplicated gateway entry,
and a transaction missing entirely from the bank side. A representative run looks like:

| Stage | Output |
| --- | --- |
| `generate_data.py` | ~75–80 ledger transactions over a 30-day window (count varies slightly run to run — it's randomized) |
| `reconciler.py` | 14 exceptions found, ~82% clean match rate |
| `agent.py` | 14/14 diagnosed (fallback mode without an API key) |
| `export_audit_sheet.py` | 14-row audit sheet, 1 of which is a `MISSING_IN_BANK` case with no ledger date/narration (by definition — flagged, not hidden) |
| `tests/eval_harness.py` | **Precision 85.71%, Recall 100.00%, F1 92.31%**, ₹29,303.33 total capital at risk, 100% of it flagged for recovery |

See the "Why precision isn't 100%" note below — it's not a bug, it's a deliberate scoring quirk worth
understanding rather than hiding.

All of the above is reproducible with a single `python main.py` run and uses only synthetic data
generated locally — nothing in `data/` or `output/` is real financial information.

---

## Known limitations

Being upfront about these matters more than pretending they don't exist:

- **Not a regulatory or compliance tool.** This project does not claim SOC 2, PCI-DSS, or any other
  compliance certification, and shouldn't be treated as one. It's a reconciliation *logic* demo.
- **Why precision isn't 100%.** The eval harness scores against a ground-truth file that only tracks
  financial-amount discrepancies. Duplicate-detection exceptions (`DUPLICATE_IN_LEDGER`,
  `DUPLICATE_IN_GATEWAY`) are real things worth flagging for human review, but since they aren't
  amount discrepancies, the harness currently scores them as false positives. Escalating them anyway
  is the right operational call; the metric just doesn't have a category for "correctly flagged
  something that wasn't a financial error."
  - A current worked example: with duplicate-handling counted as "true positive if flagged, since
    flagging it is correct" instead of "false positive by construction," precision would read 100%.
    The 85.71% figure above is the more conservative, stricter reading and is what's reported.
- **The webhook gateway can't detect UPI routing from a live payload.** Razorpay's
  `settlement.processed` webhook doesn't include a payment-method breakdown on the settlement entity
  itself (a settlement batches many underlying payment methods), so `is_upi_routed` is hardcoded to
  `False` for live webhook events — see the docstring in `webhook_receiver.py::adapt_razorpay_payload`.
  The batch reconciler (Tier 1) doesn't have this limitation, since it reads the actual bank narration.
- **Two independent copies of the fee/tax/net formula.** `reconciler.py` and `webhook_receiver.py`
  each implement the contract math separately (one batch-oriented, one single-event-oriented). They're
  each covered by their own tests, but a future contract change needs to be made in both places —
  worth consolidating into a shared `pricing.py` next.
- **In-memory idempotency store.** `webhook_receiver.py`'s duplicate-event detection
  (`PROCESSED_EVENTS`) is a Python `set()` that resets on restart — fine for a demo, not durable enough
  for production without a real store (Redis, a database table, etc.).
- **Synthetic data only.** Everything in `data/` is generated by `src/generate_data.py`
  (`random.seed(42)` for reproducibility within a run, but counts vary slightly across runs due to
  randomized batch sizes). No real merchant, bank, or payment data is included or required.
- **`tests/test_webhook_receiver.py` is written but not yet CI-verified** in the sandbox this audit was
  performed in (no network access to install `fastapi`/`httpx` there). It's straightforward FastAPI
  `TestClient` code and should be run once locally before being trusted in CI.

---

## Repository structure

```
.
├── contracts/
│   └── ochicken_contract.json       # machine-readable fee/tax contract terms
├── data/                             # synthetic ledger/gateway/ground-truth CSVs (generated)
├── src/
│   ├── generate_data.py              # synthetic data + ground truth generator
│   ├── reconciler.py                 # Tier 1: deterministic matching engine
│   ├── agent.py                      # Tier 2: batched LLM diagnostic agent
│   ├── export_audit_sheet.py         # merges Tier 1 + Tier 2 output into one audit CSV
│   ├── webhook_receiver.py           # Tier 3: FastAPI real-time webhook gateway
│   ├── app.py                        # Tier 4: Streamlit dashboard + Q&A
│   ├── qna_agent.py                  # CLI Q&A assistant (no browser needed)
│   ├── dashboard_utils.py            # pure, unit-testable dashboard/Q&A logic
│   └── alerts.py                     # Slack/Teams-style alert formatting
├── tests/
│   ├── test_reconciler.py            # 29 unit tests for the reconciliation engine
│   ├── test_dashboard_utils.py       # unit tests for dashboard KPI/Q&A logic
│   ├── test_webhook_receiver.py      # FastAPI TestClient tests for the webhook gateway
│   └── eval_harness.py               # end-to-end precision/recall/F1 evaluation
├── docs/
│   ├── architecture.md, design_decisions.md, component_pipeline.md, failure.md
│   └── PR.md                         # full engineering changelog (both hardening passes)
├── output/                           # generated audit sheet + exception reports
├── main.py                           # runs the full pipeline end to end
├── requirements.txt
├── api.env.example                   # copy to .env and fill in real values
├── pytest.ini
├── Dockerfile / docker-compose.yml
└── .github/workflows/tests.yml       # CI: pytest + pipeline smoke test on every push
```

---

## Security notes

- No real secrets are committed anywhere in this repo. `api.env.example` contains placeholder values
  only; `.env` / `api.env` are excluded via `.gitignore`.
- Webhook requests are verified with HMAC-SHA256 against `RAZORPAY_WEBHOOK_SECRET`, using
  `hmac.compare_digest` (constant-time comparison) rather than `==`, to avoid timing attacks.
- Set `WEBHOOK_STRICT_MODE=true` before deploying the webhook gateway anywhere it will receive traffic
  that matters — by default it fails open (warns + accepts) rather than closed, which is convenient
  for local development but not something you want in production.
- All financial calculations are deterministic and independent of any LLM, by design — there is no
  path by which a prompt injection in a transaction narration could alter a computed amount.

---

## License

MIT — see [`LICENSE`](LICENSE).

## A note on the project name

This repository is still hosted under `ai-finance-controller`; `LedgerLens` is the name used
throughout the code and docs going forward. If you'd like the GitHub repo itself renamed to match,
that's a one-click change under **Settings → repository name** on GitHub (not something achievable
from inside this codebase).

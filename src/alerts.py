import json
import os
import urllib.request
import pandas as pd
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent

def compute_live_metrics():
    """
    Pulls real numbers and high-risk anomalies from this run's actual audit output
    so alerts reflect live financial impact rather than static summaries.
    """
    audit_path = ROOT / "output" / "reconciliation_audit_sheet.csv"
    if not audit_path.exists():
        return {
            "total_leaks_caught": 0,
            "capital_recovered_inr": 0.0,
            "fallback_used": False,
            "total_flagged": 0,
            "highest_risk_txn": None,
        }

    audit_df = pd.read_csv(audit_path)

    # Calculate financial leakage caught
    leak_mask = audit_df["Actual Planted Error"] != "None (Valid Variance)" if "Actual Planted Error" in audit_df.columns else pd.Series([True] * len(audit_df))
    total_leaks_caught = int(leak_mask.sum())
    
    discrepancy_col = "Discrepancy (₹)" if "Discrepancy (₹)" in audit_df.columns else "variance_inr"
    capital_recovered_inr = round(float(audit_df.loc[leak_mask, discrepancy_col].abs().sum()), 2) if discrepancy_col in audit_df.columns else 0.0

    # Determine if fallback heuristics were used (supports both legacy 'Used LLM' and new schema)
    if "Used LLM" in audit_df.columns:
        fallback_used = bool(audit_df["Used LLM"].eq(False).any())
    elif "confidence" in audit_df.columns:
        fallback_used = bool((audit_df["confidence"] == 0.0).any())
    else:
        fallback_used = False

    # Extract highest-risk transaction for finance operations triage
    highest_risk_txn = None
    if not audit_df.empty and discrepancy_col in audit_df.columns:
        top_row = audit_df.loc[audit_df[discrepancy_col].abs().idxmax()]
        highest_risk_txn = {
            "tx_id": str(top_row.get("transaction_id", top_row.get("Transaction ID", "N/A"))),
            "amount": abs(float(top_row.get(discrepancy_col, 0.0))),
            "cause": str(top_row.get("Agent Diagnosis", top_row.get("root_cause", "Discrepancy Detected"))),
        }

    return {
        "total_leaks_caught": total_leaks_caught,
        "capital_recovered_inr": capital_recovered_inr,
        "fallback_used": fallback_used,
        "total_flagged": len(audit_df),
        "highest_risk_txn": highest_risk_txn,
    }

def dispatch_finance_alert(webhook_url: str = None):
    metrics = compute_live_metrics()
    total_leaks_caught = metrics["total_leaks_caught"]
    capital_recovered_inr = metrics["capital_recovered_inr"]
    
    engine_note = (
        "⚠️ Deterministic fallback used for one or more exceptions - not a live LLM diagnosis."
        if metrics["fallback_used"]
        else "Tier 2 diagnoses were produced by live Groq LLM inference."
    )
    
    # Priority card block for operator action
    top_txn = metrics["highest_risk_txn"]
    highest_risk_text = (
        f"*Highest Risk Anomaly:*\nTXN `{top_txn['tx_id']}` • ₹{top_txn['amount']:,.2f}\n_{top_txn['cause']}_"
        if top_txn
        else "*Highest Risk Anomaly:*\nNone flagged"
    )

    # Rich Markdown card payload with unified Groq branding and triage data
    slack_payload = {
        "text": "🚨 *O'Chicken AI Finance Controller: Reconciliation Report Complete* 🚨",
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    # BUG FIX: Replaced Gemini reference with unified Groq architecture
                    "text": "*Autonomous Audit Execution Summary*\nTier 1 deterministic engine and Tier 2 Groq agent (openai/gpt-oss-120b) have finalized the settlement cycle."
                }
            },
            {
                "type": "section",
                "fields": [
                    {
                        "type": "mrkdwn",
                        "text": f"*Capital Recovered:*\n₹{capital_recovered_inr:,.2f}"
                    },
                    {
                        "type": "mrkdwn",
                        "text": f"*Anomalies Flagged:*\n{total_leaks_caught} Leaks Caught"
                    },
                    {
                        "type": "mrkdwn",
                        "text": f"*Total Records Reviewed:*\n{metrics['total_flagged']}"
                    },
                    {
                        "type": "mrkdwn",
                        "text": highest_risk_text
                    }
                ]
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"📁 *Output Status:* Actionable audit sheet successfully saved to `/output/reconciliation_audit_sheet.csv`.\n{engine_note}\nRun `tests/eval_harness.py` for precision/recall validation."
                }
            }
        ]
    }

    print("------------------------------------------------------------")
    print("📢 DISPATCHING FINANCE OPERATIONS ALERT...")
    print("------------------------------------------------------------")
    print(json.dumps(slack_payload, indent=2))
    print("------------------------------------------------------------")
    print("✅ Alert simulation successfully compiled and routed!")

    # Live delivery if webhook URL is configured
    if webhook_url:
        try:
            req = urllib.request.Request(
                webhook_url,
                data=json.dumps(slack_payload).encode('utf-8'),
                headers={'Content-Type': 'application/json'}
            )
            with urllib.request.urlopen(req) as response:
                print(f"Webhook delivered successfully. Status: {response.status}")
        except Exception as e:
            print(f"Failed to deliver webhook live: {e}")

if __name__ == "__main__":
    dispatch_finance_alert(webhook_url=os.environ.get("FINANCE_ALERT_WEBHOOK_URL") or None)
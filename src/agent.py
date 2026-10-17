import json
import time
import pandas as pd
from pathlib import Path
from groq import Groq
import os
from dotenv import load_dotenv
from pydantic import BaseModel

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent

class ExceptionDiagnosis(BaseModel):
    transaction_id: str
    root_cause: str
    confidence: float
    recommended_action: str
    human_approval_required: bool
def run_tier2_agent():
    print("------------------------------------------------------------")
    print("🤖 BOOTING TIER 2: AGENTIC DIAGNOSTIC LAYER 🤖")
    print("------------------------------------------------------------")

    api_key = os.environ.get("GROQ_API_KEY")
    client = Groq(api_key=api_key) if api_key else None
    if not api_key:
        print("⚠️  No GROQ_API_KEY set in environment. Tier 2 will use the")
        print("    deterministic fallback heuristic for every exception.")

    exceptions_path = ROOT / "output" / "tier1_exceptions.csv"
    contract_path = ROOT / "contracts" / "ochicken_contract.json"
    output_path = ROOT / "output" / "agent_resolutions.csv"

    if not exceptions_path.exists():
        print("❌ Error: tier1_exceptions.csv not found. Run reconciler.py first.")
        return

    exceptions_df = pd.read_csv(exceptions_path)
    with open(contract_path, "r") as f:
        contract_data = json.load(f)

    print(f"Loaded {len(exceptions_df)} exceptions for batch analysis.")

    if client and len(exceptions_df) > 0:
        payload = exceptions_df.to_dict(orient="records")
        
        prompt = f"""
        You are an elite Enterprise AI Finance Controller. Analyze the following batch of financial reconciliation exceptions against the legal contract rules provided.
        
        Contract Rules:
        {json.dumps(contract_data, indent=2)}

        Exceptions Batch:
        {json.dumps(payload, indent=2)}

        For each record, determine the precise root cause (e.g., GATEWAY_OVERCHARGE, UPI_WAIVER, PROMO_RATE, GHOST_TXN).
        Return your response strictly as a valid JSON object with a single key 'resolutions' containing an array of objects. 
        Each object MUST have these exact keys matching our internal schema:
        - "transaction_id": string
        - "root_cause": string explaining the precise discrepancy
        - "confidence": float between 0.0 and 1.0
        - "recommended_action": string advising the finance operator (e.g., "Request refund from gateway", "Override bank ledger")
        - "human_approval_required": boolean
        
        Constraint: Do not include any LaTeX syntax, equations, or raw block formatting in your text descriptions; use plain text only.
        """

        print("Dispatching batched request to active Groq model...")
        res_df = None
        last_error = None

        for attempt in range(2):
            try:
                response = client.chat.completions.create(
                    model="openai/gpt-oss-120b",
                    messages=[
                        {"role": "system", "content": "You are a financial auditing assistant that outputs strictly valid JSON arrays."},
                        {"role": "user", "content": prompt}
                    ],
                    response_format={"type": "json_object"}
                )
                raw_content = response.choices[0].message.content
                parsed_json = json.loads(raw_content)
                
                if isinstance(parsed_json, dict):
                    resolutions = next((v for v in parsed_json.values() if isinstance(v, list)), [])
                else:
                    resolutions = parsed_json

                res_df = pd.DataFrame(resolutions)
                
                if not res_df.empty and "transaction_id" in res_df.columns:
                    merged_check = pd.merge(res_df, exceptions_df, left_on="transaction_id", right_on="transaction_id", how="left")
                    for _, row in merged_check.iterrows():
                        exc_type = str(row.get("exception_type", ""))
                        if exc_type in ["FEE_VARIANCE", "NET_MISMATCH", "MISSING_IN_GATEWAY", "MISSING_IN_BANK"]:
                            res_df.loc[res_df["transaction_id"] == row["transaction_id"], "recommended_action"] = "ESCALATE"
                            res_df.loc[res_df["transaction_id"] == row["transaction_id"], "human_approval_required"] = True
                # ----------------------------------------
                
                break
            except Exception as e:
                last_error = e
                if attempt == 0:
                    print(f"⚠️ Attempt 1 failed ({e}). Retrying once...")
                    time.sleep(2)

        if res_df is None or res_df.empty:
            print(f"❌ API batch processing failed after retry ({last_error}).")
            print("   Falling back to deterministic heuristics for this batch.")
            res_df = fallback_heuristics(exceptions_df)
    else:
        res_df = fallback_heuristics(exceptions_df)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    res_df.to_csv(output_path, index=False)

    fallback_count = (res_df["confidence"] == 0.0).sum() if "confidence" in res_df.columns else len(res_df)
    if fallback_count > 0:
        print(f"⚠️  {fallback_count}/{len(res_df)} record(s) used the fallback heuristic,")
        print(f"    not a live LLM diagnosis. See 'confidence' column in the audit sheet.")
    print(f"✅ Tier 2 Agent complete. Resolutions saved to {output_path}")

def fallback_heuristics(df):
    """
    Deterministic backstop updated to match the strict ExceptionDiagnosis schema.
    Always flags confidence as 0.0 and requires human approval.
    """
    cause_map = {
        "FEE_VARIANCE": "FEE_VARIANCE_UNRESOLVED",
        "TAX_VARIANCE": "TAX_VARIANCE_UNRESOLVED",
        "CONTRACT_VARIANCE": "CONTRACT_VARIANCE_UNRESOLVED",
        "NET_MISMATCH": "NET_MISMATCH_UNRESOLVED",
        "MISSING_IN_GATEWAY": "GHOST_TXN_BANK_SIDE",
        "MISSING_IN_BANK": "GHOST_TXN_GATEWAY_SIDE",
        "DUPLICATE_IN_LEDGER": "DUPLICATE_RECORD_BANK_SIDE",
        "DUPLICATE_IN_GATEWAY": "DUPLICATE_RECORD_GATEWAY_SIDE",
        "INVALID_DATA": "DATA_QUALITY_ISSUE",
    }
    results = []
    for _, row in df.iterrows():
        base_cause = cause_map.get(row.get("exception_type"), "UNCLASSIFIED")
        results.append({
            "transaction_id": row["transaction_id"],
            "root_cause": f"Fallback applied (No LLM diagnosis): {base_cause}",
            "confidence": 0.0,
            "recommended_action": "ESCALATE TO HUMAN REVIEW",
            "human_approval_required": True
        })
    return pd.DataFrame(results)

if __name__ == "__main__":
    run_tier2_agent()
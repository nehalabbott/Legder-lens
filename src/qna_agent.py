import os
import pandas as pd
import json
from pathlib import Path
from dotenv import load_dotenv
from groq import Groq

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent

try:
    client = Groq(api_key=os.environ.get("GROQ_API_KEY"))
except Exception as e:
    print(f"⚠️ Could not initialize Groq client ({e}). Set GROQ_API_KEY and retry.")
    client = None


def load_knowledge_base():
    """
    Loads and optimizes the audit sheet and contract for token-efficient LLM context.
    """
    audit_path = ROOT / "output" / "reconciliation_audit_sheet.csv"
    if not audit_path.exists():
        raise FileNotFoundError(
            "output/reconciliation_audit_sheet.csv not found. "
            "Run `python main.py` first to generate the audit sheet."
        )
    audit_df = pd.read_csv(audit_path)

    # Strip bulky text columns the LLM doesn't need for reasoning, and use a
    # compact JSON representation instead of a raw CSV string - meaningfully
    # cheaper on tokens for larger audit sheets.
    slim_df = audit_df.drop(columns=["Date", "Narration", "Agent Reasoning / Cause"], errors="ignore")
    optimized_audit_data = slim_df.to_dict(orient="records")

    with open(ROOT / "contracts" / "ochicken_contract.json", "r") as f:
        contract = json.load(f)

    return optimized_audit_data, contract


def run_finance_chat():
    if client is None:
        print("❌ Cannot start Q&A agent: Groq client not initialized. Set GROQ_API_KEY.")
        return

    try:
        optimized_audit_data, contract = load_knowledge_base()
    except FileNotFoundError as e:
        print(f"❌ {e}")
        return

    system_instruction = f"""You are the LedgerLens Financial Q&A Assistant.
You answer user questions based EXCLUSIVELY on this merchant contract and the audit data below.

Contract Terms:
{json.dumps(contract)}

Audit Exceptions Data:
{json.dumps(optimized_audit_data)}

Rules:
1. Trace all answers back to specific Transaction IDs that appear in the data above.
2. Never invent transaction IDs, amounts, dates, or root causes that are not present in this data.
   If asked about a transaction that isn't in the data above, say so explicitly instead of guessing.
3. Clearly explain the math or contract clause that justifies your answer.
4. Be concise and professional.
"""

    print("Booting Settlement Q&A Agent... \n")

    # Manually maintain message history for the stateless Groq API
    messages = [{"role": "system", "content": system_instruction}]

    print("Agent ready! Ask about your financial data (type 'exit' to quit).")
    print("Example: 'Why was transaction 1179 escalated?' or 'How much money are we losing to gateway overcharges?'\n")

    while True:
        user_query = input("Finance Team: ")
        if user_query.lower() in ["exit", "quit"]:
            print("Shutting down Q&A agent.")
            break
            
        messages.append({"role": "user", "content": user_query})

        try:
            # Replaced gemini-3.6-flash with the judge-approved openai/gpt-oss-120b
            response_stream = client.chat.completions.create(
                model="openai/gpt-oss-120b",
                messages=messages,
                stream=True
            )
            
            print(f"\nAI Controller: ", end="", flush=True)
            full_response = ""
            for chunk in response_stream:
                if chunk.choices[0].delta.content:
                    text = chunk.choices[0].delta.content
                    print(text, end="", flush=True)
                    full_response += text
            print("\n\n" + "-" * 50)
            
            # Append the completed AI response to the history array to maintain context
            messages.append({"role": "assistant", "content": full_response})
            
        except Exception as e:
            print(f"\nError processing query: {e}\n")


if __name__ == "__main__":
    run_finance_chat()
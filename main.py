import subprocess
import sys
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()  # loads GROQ_API_KEY / RAZORPAY_WEBHOOK_SECRET / etc. from .env,
                # then propagates into the child processes spawned below.

ROOT = Path(__file__).resolve().parent

def run_pipeline():
    print("="*60)
    print("🚀 BOOTING LEDGERLENS RECONCILIATION PIPELINE 🚀")
    print("="*60)

    scripts = [
        ("1. Generating Synthetic Data...", "src/generate_data.py"),
        ("2. Running Deterministic Reconciler (Tier 1)...", "src/reconciler.py"),
        ("3. Running AI Diagnostic Agent (Tier 2)...", "src/agent.py"),
        ("4. Exporting Enterprise Audit Sheet...", "src/export_audit_sheet.py"),
        ("5. Evaluating Accuracy Metrics...", "tests/eval_harness.py"),
        ("6. Dispatching Operational Finance Alert...", "src/alerts.py"),
    ]

    for step_name, script_path in scripts:
        print(f"\n>> {step_name}")
        try:
            subprocess.run([sys.executable, str(ROOT / script_path)], check=True)
        except subprocess.CalledProcessError as e:
            print(f"\n❌ Pipeline halted at {script_path}. Error code: {e.returncode}")
            sys.exit(1)
    
    print("\n" + "="*60)
    print("✅ PIPELINE COMPLETE! All reports saved to the /output folder.")
    print("💡 To interact with the CLI audit Q&A, run: python src/qna_agent.py")
    print("📊 To view the Finance Operator Dashboard, run: streamlit run src/app.py")
    print("🌐 To start the real-time API daemon, run: python -m uvicorn src.webhook_receiver:app --reload")
    print("="*60)

if __name__ == "__main__":
    run_pipeline()
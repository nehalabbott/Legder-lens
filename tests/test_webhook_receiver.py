"""
Tests for src/webhook_receiver.py (Tier 3 FastAPI gateway).

Requires fastapi + httpx (fastapi.testclient.TestClient), both in
requirements.txt. These could not be executed in the sandbox this project
was reviewed in (no network access to install fastapi/httpx/groq), so treat
this file as reviewed-but-not-yet-CI-verified until it's run once locally:

    pip install -r requirements.txt
    pytest tests/test_webhook_receiver.py -v

Run with: pytest tests/test_webhook_receiver.py -v
"""
import hashlib
import hmac
import json

import pytest

fastapi_testclient = pytest.importorskip("fastapi.testclient")
TestClient = fastapi_testclient.TestClient

SETTLEMENT_SECRET = "test_secret_123"


def _sign(body: bytes, secret: str = SETTLEMENT_SECRET) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _sample_payload(event_id_suffix="1", amount_paise=1_000_000, amount_settled_paise=982_400):
    return {
        "event": "settlement.processed",
        "payload": {
            "settlement": {
                "entity": {
                    "id": f"setl_test_{event_id_suffix}",
                    "amount": amount_paise,
                    "fees": 18000,
                    "tax": 3240,
                    "amount_settled": amount_settled_paise,
                    "created_at": 1753776000,  # 2025-07-29, well outside the 2026 promo window
                }
            }
        },
    }


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", SETTLEMENT_SECRET)
    monkeypatch.delenv("WEBHOOK_STRICT_MODE", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    from src import webhook_receiver
    webhook_receiver.PROCESSED_EVENTS.clear()
    return TestClient(webhook_receiver.app)


def test_health_check(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.json()["status"] == "active"


def test_missing_headers_rejected(client):
    resp = client.post("/api/v1/reconcile-webhook", json=_sample_payload())
    assert resp.status_code == 400


def test_invalid_signature_rejected(client):
    body = json.dumps(_sample_payload()).encode()
    resp = client.post(
        "/api/v1/reconcile-webhook",
        content=body,
        headers={"X-Razorpay-Signature": "deadbeef", "X-Razorpay-Event-Id": "evt_1"},
    )
    assert resp.status_code == 400


def test_valid_signature_accepted_and_reconciled(client):
    payload = _sample_payload()
    body = json.dumps(payload).encode()
    sig = _sign(body)
    resp = client.post(
        "/api/v1/reconcile-webhook",
        content=body,
        headers={"X-Razorpay-Signature": sig, "X-Razorpay-Event-Id": "evt_2"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["transaction_id"] == "setl_test_1"
    # amount 10000, standard 1.8% MDR => fee 180, gst 32.4, expected net 9787.6
    # amount_settled_paise 982400 => 9824.00, so this IS a variance case.
    assert data["status"] == "EXCEPTION_FLAGGED"


def test_duplicate_event_id_ignored(client):
    payload = _sample_payload(event_id_suffix="dup")
    body = json.dumps(payload).encode()
    sig = _sign(body)
    headers = {"X-Razorpay-Signature": sig, "X-Razorpay-Event-Id": "evt_dup"}

    first = client.post("/api/v1/reconcile-webhook", content=body, headers=headers)
    second = client.post("/api/v1/reconcile-webhook", content=body, headers=headers)

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["status"] == "ignored"
    assert second.json()["reason"] == "Duplicate event ID"


def test_strict_mode_refuses_when_secret_missing(monkeypatch):
    monkeypatch.delenv("RAZORPAY_WEBHOOK_SECRET", raising=False)
    monkeypatch.setenv("WEBHOOK_STRICT_MODE", "true")
    from src import webhook_receiver
    import importlib
    importlib.reload(webhook_receiver)  # re-read STRICT_MODE from env
    webhook_receiver.PROCESSED_EVENTS.clear()
    client = TestClient(webhook_receiver.app)

    body = json.dumps(_sample_payload(event_id_suffix="strict")).encode()
    resp = client.post(
        "/api/v1/reconcile-webhook",
        content=body,
        headers={"X-Razorpay-Signature": "anything", "X-Razorpay-Event-Id": "evt_strict"},
    )
    assert resp.status_code == 500

    # Reset module state for any tests that run after this one in the same process.
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", SETTLEMENT_SECRET)
    monkeypatch.setenv("WEBHOOK_STRICT_MODE", "false")
    importlib.reload(webhook_receiver)

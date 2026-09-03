"""Fire one real callback per provider at the running server and report.

    python manage.py runserver 127.0.0.1:8009 --insecure --settings=config.settings.preview
    python tools/probe_providers.py

Phase 5 built this for Octo and Multicard, because a handler can be perfectly
wired and still answer 500 to the only request that matters. The other four
had never been driven end to end at all — the unit tests use Payme's shape
for everything — so this asks each of the six the one question that counts:
if this provider confirmed a payment right now, would we record it?
"""
import base64
import hashlib
import json
import os
import pathlib
import sys
import urllib.error
import urllib.request
import uuid as uuid_module

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")

import django  # noqa: E402

django.setup()
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from decimal import Decimal  # noqa: E402

from merchants.models import SellerProfile  # noqa: E402
from payments.models import Transaction, TransactionStatus  # noqa: E402
from payments.services import save_credentials, set_enabled, set_test_mode  # noqa: E402

BASE = os.environ.get("CRAWL_BASE", "http://127.0.0.1:8009")
AMOUNT = Decimal("50000")

# Every provider id has to be new on every run. tolov stores one row per
# (gateway, transaction_id) and treats a repeat as a replay — correctly. With
# fixed ids a second run looks like a replayed callback, the handler
# short-circuits without touching the fresh transaction, and the probe reports
# a defect that is entirely its own.
RUN = uuid_module.uuid4().hex[:10]

CREDENTIALS = {
    "payme": {"payme_id": "probe-payme-id", "payme_key": "probe-payme-key"},
    "click": {"service_id": "12345", "merchant_id": "9001",
              "merchant_user_id": "9002", "secret_key": "probe-click-secret"},
    "uzum": {"service_id": "54321", "username": "probe-uzum-user",
             "password": "probe-uzum-pass"},
    "paynet": {"merchant_id": "7001", "service_id": "260",
               "username": "probe-paynet-user", "password": "probe-paynet-pass"},
}


def profile():
    seller = SellerProfile.objects.filter(status="approved").first()
    seller.approve()
    for provider, credentials in CREDENTIALS.items():
        save_credentials(seller, provider, credentials)
        set_enabled(seller, provider, True)
        set_test_mode(seller, provider, False)
    return seller


def fresh_transaction(seller, provider):
    return Transaction.objects.create(
        seller=seller, provider=provider, amount=AMOUNT,
        status=TransactionStatus.PENDING, is_test_mode=False,
    )


def post(path, body, headers=None, form=False):
    data = body.encode("utf-8") if isinstance(body, str) else json.dumps(body).encode()
    request = urllib.request.Request(
        f"{BASE}{path}", data=data, method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded" if form
            else "application/json",
            **(headers or {}),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        return None, f"{type(exc).__name__}: {exc}"


def basic(user, password):
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def report(name, status, body, txn):
    txn.refresh_from_db()
    ok = status == 200 and txn.status == TransactionStatus.PAID
    mark = "PASS" if ok else "FAIL"
    print(f"  {mark}  {name:26} HTTP {status}  -> {txn.status}")
    if not ok:
        print(f"        {body[:220]}")
    return ok


def main():
    seller = profile()
    print(f"seller {seller.uid}, {BASE}\n")
    results = []

    # --- Payme: JSON-RPC, Basic auth on the key, tiyin -------------------
    txn = fresh_transaction(seller, "payme")
    url = f"/webhooks/payme/{seller.uid}/"
    header = basic("Paycom", CREDENTIALS["payme"]["payme_key"])
    payme_id = uuid_module.uuid4().hex[:24]
    post(url, {"jsonrpc": "2.0", "id": 1, "method": "CreateTransaction",
               "params": {"id": payme_id, "time": 1756800000000,
                          "amount": int(AMOUNT * 100),
                          "account": {"id": str(txn.pk)}}}, header)
    status, body = post(url, {"jsonrpc": "2.0", "id": 2,
                              "method": "PerformTransaction",
                              "params": {"id": payme_id}}, header)
    results.append(report("payme succeeded", status, body, txn))

    # --- Click: form-encoded, md5 over eight parts, soum -----------------
    txn = fresh_transaction(seller, "click")
    url = f"/webhooks/click/{seller.uid}/"
    secret = CREDENTIALS["click"]["secret_key"]
    service = CREDENTIALS["click"]["service_id"]
    click_id, sign_time = f"34{RUN}", "2026-09-02 12:00:00"

    def click_body(action, prepare_id, error="0"):
        raw = (f"{click_id}{service}{secret}{txn.pk}{prepare_id}"
               f"{AMOUNT}{action}{sign_time}")
        sign = hashlib.md5(raw.encode()).hexdigest()
        return (f"click_trans_id={click_id}&service_id={service}"
                f"&merchant_trans_id={txn.pk}&merchant_prepare_id={prepare_id}"
                f"&amount={AMOUNT}&action={action}&error={error}"
                f"&sign_time={sign_time}&sign_string={sign}")

    status, body = post(url, click_body("0", ""), form=True)
    prepare_id = ""
    try:
        prepare_id = json.loads(body).get("merchant_prepare_id", "")
    except Exception:  # noqa: BLE001
        pass
    status, body = post(url, click_body("1", prepare_id), form=True)
    results.append(report("click succeeded", status, body, txn))

    # --- Click failure: the path that writes a reason --------------------
    # Click's real decline is Prepare, then Complete carrying a negative
    # error code — not a bare Complete. Skipping the Prepare tests nothing.
    txn_failed = fresh_transaction(seller, "click")
    failed_id = f"99{RUN}"

    def failed_body(action, prepare_id, error):
        raw = (f"{failed_id}{service}{secret}{txn_failed.pk}{prepare_id}"
               f"{AMOUNT}{action}{sign_time}")
        sign = hashlib.md5(raw.encode()).hexdigest()
        return (f"click_trans_id={failed_id}&service_id={service}"
                f"&merchant_trans_id={txn_failed.pk}"
                f"&merchant_prepare_id={prepare_id}"
                f"&amount={AMOUNT}&action={action}&error={error}"
                f"&sign_time={sign_time}&sign_string={sign}")

    _status, body = post(url, failed_body("0", "", "0"), form=True)
    try:
        prepare_id = json.loads(body).get("merchant_prepare_id", "")
    except Exception:  # noqa: BLE001
        prepare_id = ""
    status, body = post(url, failed_body("1", prepare_id, "-5017"), form=True)
    txn_failed.refresh_from_db()
    ok = txn_failed.status in (TransactionStatus.FAILED,
                               TransactionStatus.CANCELLED)
    mark = "PASS" if ok else "FAIL"
    print(f"  {mark}  {'click declined':26} HTTP {status}  -> "
          f"{txn_failed.status}")
    if not ok:
        print(f"        {body[:220]}")
    results.append(ok)

    # --- Uzum: JSON, Basic auth, operation is the last path segment ------
    txn = fresh_transaction(seller, "uzum")
    base_url = f"/webhooks/uzum/{seller.uid}"
    header = basic(CREDENTIALS["uzum"]["username"], CREDENTIALS["uzum"]["password"])
    trans_id = str(uuid_module.uuid4())
    body_create = {"serviceId": int(CREDENTIALS["uzum"]["service_id"]),
                   "timestamp": 1756800000000, "transId": trans_id,
                   "amount": int(AMOUNT * 100),
                   "params": {"orderId": str(txn.pk)}}
    post(f"{base_url}/check/", body_create, header)
    post(f"{base_url}/create/", body_create, header)
    status, body = post(f"{base_url}/confirm/", body_create, header)
    results.append(report("uzum confirmed", status, body, txn))

    # A callback URL configured without the operation must say so, not 500.
    status, body = post(base_url + "/", body_create, header)
    print(f"  ----  uzum with no operation    HTTP {status}")
    if status is None or status >= 500:
        print(f"        {body[:220]}")

    # --- Paynet: JSON-RPC, Basic auth, tiyin -----------------------------
    txn = fresh_transaction(seller, "paynet")
    url = f"/webhooks/paynet/{seller.uid}/"
    header = basic(CREDENTIALS["paynet"]["username"],
                   CREDENTIALS["paynet"]["password"])
    status, body = post(url, {
        "jsonrpc": "2.0", "id": 102, "method": "PerformTransaction",
        "params": {"transactionId": f"77{RUN}",
                   "serviceId": int(CREDENTIALS["paynet"]["service_id"]),
                   "amount": int(AMOUNT * 100), "time": 1756800000000,
                   "fields": {"id": str(txn.pk)}},
    }, header)
    results.append(report("paynet performed", status, body, txn))

    print()
    if all(results):
        print(f"OK - all {len(results)} providers recorded the payment")
        return 0
    print(f"{results.count(False)} of {len(results)} providers did NOT record it")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Dev-only: drive real signed provider callbacks at the running server.

The unit tests use Django's test client, which bypasses the real WSGI stack.
This posts over actual HTTP so middleware, URL routing, CSRF exemption and the
JSON parsing are all exercised the way a provider will exercise them.

Kept rather than thrown away: three of the defects it caught — the Decimal
that made every live Octo callback answer 500, the Multicard handler that
refused to be constructed at all, and the AccountNotFound that came out of a
Multicard callback as an unhandled exception — were all invisible to the test
client and visible here in one run.

    python manage.py runserver 127.0.0.1:8000 --noreload
    python tools/live_callbacks.py

It writes to the dev database and needs seller 73f7ak to exist.
"""
import hashlib
import json
import os
import pathlib
import sys
import urllib.request
import uuid as uuid_module

import django

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")
django.setup()

from decimal import Decimal  # noqa: E402

from merchants.models import SellerProfile  # noqa: E402
from payments.models import Transaction, TransactionStatus  # noqa: E402
from payments.services import save_credentials, set_enabled, set_test_mode  # noqa: E402

BASE = os.environ.get("CRAWL_BASE", "http://127.0.0.1:8000")
SHOP, SECRET, KEY = "77001", "octo-secret", "octo-unique-key-live"

profile = SellerProfile.objects.get(uid="73f7ak")
profile.approve()
save_credentials(
    profile,
    "octo",
    {"octo_shop_id": SHOP, "octo_secret": SECRET, "octo_unique_key": KEY},
)
set_enabled(profile, "octo", True)
# Live mode, so signatures are actually verified.
set_test_mode(profile, "octo", False)

url = f"{BASE}/webhooks/octo/{profile.uid}/"

# Octo's uuid is what makes a callback idempotent, so tolov keeps a row per
# uuid for ever. Fixed ids would make every run after the first look like a
# replay of the last one — the script would pass once and then "fail" while
# nothing was wrong. A run stamp keeps each run's callbacks its own.
RUN = uuid_module.uuid4().hex[:8]


def sign(uuid, status, key=KEY):
    return hashlib.sha1(f"{key}{uuid}{status}".encode("utf-8")).hexdigest()


def post(payload):
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def callback(txn, status, uuid, signature=None, amount=None):
    uuid = f"{uuid}-{RUN}"
    return post(
        {
            "octo_payment_UUID": uuid,
            "shop_transaction_id": str(txn.pk),
            "status": status,
            "total_sum": float(amount if amount is not None else txn.amount),
            "signature": signature if signature is not None else sign(uuid, status),
        }
    )


def fresh(amount="42000"):
    return Transaction.objects.create(
        seller=profile, provider="octo", amount=Decimal(amount),
        status=TransactionStatus.PENDING, is_test_mode=False,
    )


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: {got!r}" + ("" if ok else f" (wanted {want!r})"))
    return ok


results = []
print("Octo over real HTTP\n")

# 1. A correctly signed success.
t = fresh()
code, _ = callback(t, "succeeded", "live-1")
t.refresh_from_db()
results.append(check("signed success -> HTTP", code, 200))
results.append(check("signed success -> status", t.status, TransactionStatus.PAID))

# 2. A forged signature.
t2 = fresh()
code, _ = callback(t2, "succeeded", "live-2", signature="0" * 40)
t2.refresh_from_db()
results.append(check("forged signature -> HTTP", code, 403))
results.append(check("forged signature -> untouched", t2.status, TransactionStatus.PENDING))

# 3. An amount that does not match our row.
t3 = fresh()
code, _ = callback(t3, "succeeded", "live-3", amount="1")
t3.refresh_from_db()
results.append(check("wrong amount -> HTTP", code, 400))
results.append(check("wrong amount -> untouched", t3.status, TransactionStatus.PENDING))

# 4. A replay of the successful one.
code, _ = callback(t, "succeeded", "live-1")
t.refresh_from_db()
results.append(check("replay -> HTTP", code, 200))
results.append(check("replay -> still paid", t.status, TransactionStatus.PAID))

# 5. A refund of the successful one.
code, _ = callback(t, "refunded", "live-1")
t.refresh_from_db()
results.append(check("refund -> HTTP", code, 200))
results.append(check("refund -> status", t.status, TransactionStatus.REFUNDED))

# 6. A cancellation.
t4 = fresh()
code, _ = callback(t4, "canceled", "live-4")
t4.refresh_from_db()
results.append(check("cancel -> HTTP", code, 200))
results.append(check("cancel -> status", t4.status, TransactionStatus.CANCELLED))

# 6b. A card the bank declined. Octo funnels this into the same hook as a
# cancellation, and the two are different things to a seller.
t5 = fresh()
code, _ = callback(t5, "failed", "live-5")
t5.refresh_from_db()
results.append(check("declined -> HTTP", code, 200))
results.append(check("declined -> status", t5.status, TransactionStatus.FAILED))

# 6c. A partial refund. The payment still stands for the rest, and we have
# nowhere to write down half a reversal — so the status must not move.
t6 = fresh()
callback(t6, "succeeded", "live-6")
code, _ = post(
    {
        "octo_payment_UUID": f"live-6-{RUN}",
        "shop_transaction_id": str(t6.pk),
        "status": "refunded",
        "total_sum": float(t6.amount),
        "refunded_sum": 1000,
        "signature": sign(f"live-6-{RUN}", "refunded"),
    }
)
t6.refresh_from_db()
results.append(check("partial refund -> HTTP", code, 200))
results.append(check("partial refund -> still paid", t6.status, TransactionStatus.PAID))

# 7. An unknown seller.
body = json.dumps({"octo_payment_UUID": "x", "shop_transaction_id": "1",
                   "status": "succeeded", "total_sum": 1, "signature": "x"}).encode()
req = urllib.request.Request(f"{BASE}/webhooks/octo/zzzzzz/", data=body,
                             headers={"Content-Type": "application/json"})
try:
    with urllib.request.urlopen(req) as r:
        code = r.status
except urllib.error.HTTPError as exc:
    code = exc.code
results.append(check("unknown seller -> HTTP", code, 404))


# --- Multicard --------------------------------------------------------------
# Its tolov handler raised in __init__ until settings carried a placeholder
# secret, so until now every one of these answered 500 over real HTTP.

print("\nMulticard over real HTTP\n")

MC = {"application_id": "app-live", "secret": "mc-secret-live", "store_id": "9001"}
save_credentials(profile, "multicard", MC)
set_enabled(profile, "multicard", True)
set_test_mode(profile, "multicard", False)
mc_url = f"{BASE}/webhooks/multicard/{profile.uid}/"


def mc_post(payload):
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        mc_url, data=body, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code


def mc_callback(txn, *, tiyin=None, sign_value=None, store=None):
    store = store or MC["store_id"]
    amount = tiyin if tiyin is not None else int(txn.amount * 100)
    raw = f"{store}{txn.pk}{amount}{MC['secret']}"
    return mc_post(
        {
            "store_id": store,
            "invoice_id": txn.pk,
            "amount": amount,
            "uuid": f"mc-{RUN}-{txn.pk}",
            "sign": sign_value or hashlib.md5(raw.encode("utf-8")).hexdigest(),
        }
    )


def mc_fresh(amount="42000"):
    return Transaction.objects.create(
        seller=profile, provider="multicard", amount=Decimal(amount),
        status=TransactionStatus.PENDING, is_test_mode=False,
    )


m1 = mc_fresh()
code = mc_callback(m1)
m1.refresh_from_db()
results.append(check("signed success -> HTTP", code, 200))
results.append(check("signed success -> status", m1.status, TransactionStatus.PAID))

m2 = mc_fresh()
code = mc_callback(m2, sign_value="0" * 32)
m2.refresh_from_db()
results.append(check("forged signature -> HTTP", code, 403))
results.append(check("forged signature -> untouched", m2.status, TransactionStatus.PENDING))

# Correctly signed for an amount that is not ours. tolov's Multicard handler
# never compares it; our own guard is the only thing standing here.
m3 = mc_fresh()
code = mc_callback(m3, tiyin=100)
m3.refresh_from_db()
results.append(check("wrong amount -> HTTP", code, 200))
results.append(check("wrong amount -> untouched", m3.status, TransactionStatus.PENDING))

code = mc_callback(m1)
m1.refresh_from_db()
results.append(check("replay -> HTTP", code, 200))
results.append(check("replay -> still paid", m1.status, TransactionStatus.PAID))

# An invoice that does not exist. tolov's handler has no try/except at all, so
# this used to leave the view as an unhandled exception.
ghost_raw = f"{MC['store_id']}99999995000000{MC['secret']}"
code = mc_post({"store_id": MC["store_id"], "invoice_id": 9999999,
                "amount": 5000000, "uuid": f"mc-ghost-{RUN}",
                "sign": hashlib.md5(ghost_raw.encode("utf-8")).hexdigest()})
results.append(check("unknown invoice -> HTTP", code, 404))

print(f"\n{sum(results)}/{len(results)} checks passed")
sys.exit(0 if all(results) else 1)

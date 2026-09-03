"""Do the credentials in .env actually work? Read-only.

    python tools/check_credentials.py

Every check here is a *read*. Nothing sends an SMS, nothing posts a Telegram
message, nothing takes a payment. A credential that is present is not a
credential that works, and the two ways to find out are this or a customer
standing at a till.

It never prints a secret. A wrong value is reported by what it failed to do,
not by echoing it.
"""
import os
import pathlib
import sys

import httpx

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")

import django  # noqa: E402

django.setup()
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from django.conf import settings  # noqa: E402

problems = []
warnings = []


def ok(label, detail=""):
    print(f"  ok    {label}" + (f" — {detail}" if detail else ""))


def fail(label, detail):
    problems.append(f"{label}: {detail}")
    print(f"  FAIL  {label} — {detail}")


def warn(label, detail):
    warnings.append(f"{label}: {detail}")
    print(f"  warn  {label} — {detail}")


def check_telegram():
    print("Telegram")
    token = settings.TELEGRAM_BOT_TOKEN
    chat_id = settings.TELEGRAM_CHAT_ID
    if not token:
        fail("bot token", "not set — card orders and contact messages go nowhere")
        return
    if not chat_id:
        fail("chat id", "not set — the bot has nobody to notify")
        return

    base = f"https://api.telegram.org/bot{token}"
    try:
        me = httpx.get(f"{base}/getMe", timeout=15).json()
    except Exception as exc:  # noqa: BLE001
        fail("bot token", f"could not reach Telegram: {type(exc).__name__}")
        return
    if not me.get("ok"):
        fail("bot token", f"Telegram rejected it: {me.get('description')}")
        return
    ok("bot token", f"@{me['result'].get('username')}")

    # getChat proves the bot can see the chat. A bot that has never been
    # spoken to cannot message a private chat — Telegram forbids it — and
    # that failure would otherwise only appear on the first real card order.
    try:
        chat = httpx.get(f"{base}/getChat", params={"chat_id": chat_id},
                         timeout=15).json()
    except Exception as exc:  # noqa: BLE001
        fail("chat id", f"could not reach Telegram: {type(exc).__name__}")
        return
    if not chat.get("ok"):
        fail(
            "chat id",
            f"{chat.get('description')} — open a chat with the bot and send "
            "it /start, or add it to the group",
        )
        return
    result = chat["result"]
    ok("chat id", f"{result.get('type')} "
                  f"'{result.get('title') or result.get('first_name', '')}'")


def check_eskiz():
    print("\nEskiz")
    email = settings.ESKIZ_EMAIL
    password = settings.ESKIZ_PASSWORD
    if not (email and password):
        fail("credentials", "not set — no OTP can be delivered in production")
        return

    base = settings.ESKIZ_BASE_URL.rstrip("/")
    try:
        response = httpx.post(f"{base}/auth/login",
                              data={"email": email, "password": password},
                              timeout=20)
    except Exception as exc:  # noqa: BLE001
        fail("login", f"could not reach Eskiz: {type(exc).__name__}")
        return
    if response.status_code != 200:
        fail("login", f"HTTP {response.status_code} — check the email and password")
        return
    token = (response.json().get("data") or {}).get("token")
    if not token:
        fail("login", "succeeded but returned no token")
        return
    ok("login", f"authenticated as {email}")

    headers = {"Authorization": f"Bearer {token}"}
    try:
        user = httpx.get(f"{base}/auth/user", headers=headers, timeout=20).json()
    except Exception as exc:  # noqa: BLE001
        warn("account", f"could not read the account: {type(exc).__name__}")
        return
    data = user.get("data") or {}
    balance = data.get("balance")
    status = data.get("status")
    ok("account", f"status={status} balance={balance}")
    if status and str(status).lower() not in ("active", "1"):
        warn("account", f"status is {status!r}, not active")
    if balance is not None and float(balance) <= 0:
        warn("balance", f"{balance} — no SMS can be sent until it is topped up")

    # The sender id has to be one Eskiz has approved for this account. 4546 is
    # their shared test sender and works for everyone; a custom one that is
    # not registered fails only at send time, on a real user's registration.
    sender = settings.ESKIZ_SENDER
    if sender == "4546":
        ok("sender", "4546 (Eskiz's shared sender)")
    else:
        warn("sender", f"{sender!r} — confirm Eskiz has approved this sender id")


def check_site_url():
    print("\nURLs handed to providers")
    site = settings.SITE_URL
    if site.startswith("http://127.0.0.1") or site.startswith("http://localhost"):
        warn(
            "SITE_URL",
            f"{site} — fine for development. In production this is the address "
            "Octo is told to send its callback to, so a payment confirmed "
            "against localhost is a payment never recorded.",
        )
    elif not site.startswith("https://"):
        fail("SITE_URL", f"{site} — providers require HTTPS for callbacks")
    else:
        ok("SITE_URL", site)


def check_sms_backend():
    print("\nSMS backend")
    backend = getattr(settings, "SMS_BACKEND", "console")
    if backend == "console":
        ok("dev", "console — codes print to the runserver output, none are sent")
    elif backend == "eskiz":
        ok("prod", "eskiz")
    else:
        fail("SMS_BACKEND", f"{backend!r} is not a backend that exists")


def main():
    print(f"settings: {os.environ['DJANGO_SETTINGS_MODULE']}\n")
    check_telegram()
    check_eskiz()
    check_site_url()
    check_sms_backend()

    print()
    for line in warnings:
        print(f"  warn: {line}")
    if problems:
        print(f"\n{len(problems)} credential problem(s):")
        for line in problems:
            print(f"  {line}")
        return 1
    print("OK - every credential that is set works")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

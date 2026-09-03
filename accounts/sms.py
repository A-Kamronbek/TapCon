"""SMS delivery.

One tiny interface with two implementations, chosen by ``settings.SMS_BACKEND``:
console in development, Eskiz in production. Nothing else in the codebase
talks to an SMS provider directly.
"""
import logging
import threading

import httpx
from django.conf import settings

logger = logging.getLogger("accounts.sms")


class SMSError(Exception):
    """Delivery failed. The caller decides whether that is fatal."""


def to_eskiz_phone(phone: str) -> str:
    """'+998 90 123 45 67' -> '998901234567' (Eskiz wants digits only)."""
    return "".join(ch for ch in phone if ch.isdigit())


class BaseSMSBackend:
    def send(self, phone: str, message: str) -> None:
        raise NotImplementedError


class ConsoleSMSBackend(BaseSMSBackend):
    """Development backend: prints the message instead of spending credits."""

    def send(self, phone: str, message: str) -> None:
        print(f"\n--- SMS to {phone} ---\n{message}\n---\n", flush=True)


class EskizSMSBackend(BaseSMSBackend):
    """Eskiz.uz (https://notify.eskiz.uz/api).

    Flow: POST /auth/login {email, password} -> data.token, then
    POST /message/sms/send {mobile_phone, message, from} with a bearer token.
    Tokens expire, so a 401 triggers one re-login and a single retry.

    Note: Eskiz only delivers message texts that have passed their
    moderation. Until TapCon's OTP template is approved, the only text that
    goes through is their fixed test string ("Bu Eskiz dan test"), so use the
    console backend for real testing.
    """

    def __init__(self):
        self.base_url = settings.ESKIZ_BASE_URL.rstrip("/")
        self.email = settings.ESKIZ_EMAIL
        self.password = settings.ESKIZ_PASSWORD
        self.sender = settings.ESKIZ_SENDER
        self._token = None
        self._lock = threading.Lock()

    def _login(self) -> str:
        response = httpx.post(
            f"{self.base_url}/auth/login",
            data={"email": self.email, "password": self.password},
            timeout=15,
        )
        if response.status_code != 200:
            raise SMSError(f"Eskiz login failed: HTTP {response.status_code}")
        token = (response.json().get("data") or {}).get("token")
        if not token:
            raise SMSError("Eskiz login returned no token")
        return token

    def _get_token(self, force_refresh: bool = False) -> str:
        with self._lock:
            if force_refresh or not self._token:
                self._token = self._login()
            return self._token

    def _post_sms(self, phone: str, message: str, token: str):
        return httpx.post(
            f"{self.base_url}/message/sms/send",
            headers={"Authorization": f"Bearer {token}"},
            data={
                "mobile_phone": to_eskiz_phone(phone),
                "message": message,
                "from": self.sender,
            },
            timeout=20,
        )

    def send(self, phone: str, message: str) -> None:
        if not (self.email and self.password):
            raise SMSError("Eskiz credentials are not configured")

        response = self._post_sms(phone, message, self._get_token())
        if response.status_code == 401:
            response = self._post_sms(phone, message, self._get_token(force_refresh=True))

        if response.status_code not in (200, 201):
            # Never log the message body: it contains the OTP code.
            raise SMSError(f"Eskiz send failed: HTTP {response.status_code}")

        logger.info("SMS sent to %s", phone)


_BACKENDS = {
    "console": ConsoleSMSBackend,
    "eskiz": EskizSMSBackend,
}

_instance = None


def get_sms_backend() -> BaseSMSBackend:
    global _instance
    if _instance is None:
        name = getattr(settings, "SMS_BACKEND", "console")
        try:
            _instance = _BACKENDS[name]()
        except KeyError as exc:
            raise SMSError(f"Unknown SMS_BACKEND: {name!r}") from exc
    return _instance


def send_sms(phone: str, message: str) -> None:
    get_sms_backend().send(phone, message)

"""Rate limits: that they refuse, and — more important — that they don't.

A limit set too tight is not a smaller problem than no limit at all. It is a
different one, and on a payments product a worse one: a provider whose
callback we refuse means a seller who was paid and never told, and a customer
turned away at a till is a lost sale that looks like a broken product.

So each limit is tested from both ends: that a script hits it, and that the
worst realistic burst from a real person or a real provider does not.
"""
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase, override_settings

from accounts.services import register_seller
from core import throttle
from merchants.models import SellerProfile
from payments.services import save_credentials, set_enabled

PHONE = "+998 90 123 45 67"
PASSWORD = "s3cretpw!x"

# The limits are counted in a shared cache, so every test starts from zero or
# it inherits whatever the last one left behind.
LOCAL_CACHE = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "throttle-tests",
    }
}


@override_settings(CACHES=LOCAL_CACHE)
class ThrottleMechanicsTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_it_allows_up_to_the_limit_then_refuses(self):
        results = [throttle.hit("t", "a", limit=3, period=60)[0] for _ in range(5)]
        self.assertEqual(results, [True, True, True, False, False])

    def test_callers_are_counted_separately(self):
        for _ in range(3):
            throttle.hit("t", "a", limit=3, period=60)
        allowed = throttle.hit("t", "b", limit=3, period=60)[0]
        self.assertTrue(allowed, "one caller's flood must not lock out another")

    def test_scopes_are_counted_separately(self):
        for _ in range(3):
            throttle.hit("login", "a", limit=3, period=60)
        allowed = throttle.hit("sms", "a", limit=3, period=60)[0]
        self.assertTrue(allowed)

    def test_a_flood_is_logged_once_not_once_per_request(self):
        """Ten thousand refusals must not write ten thousand log lines.

        Filling a small VPS's disk is a better outcome for an attacker than
        the flood itself, so only the request that first crosses the line is
        worth a warning.
        """
        url = "/uz/contact/"
        with self.assertLogs("core.throttle", level="WARNING") as captured:
            for _ in range(40):
                self.client.post(url, {"full_name": "x"})
        self.assertEqual(
            len(captured.output), 1,
            f"expected one warning per window, got {len(captured.output)}",
        )

    def test_an_enormous_identity_still_counts(self):
        """The OTP limits are keyed on an unvalidated POST field.

        Pasted into the cache key raw, a 300-character phone number would
        overflow the 255-character key column; the write would fail, this
        module would fail open, and the limit would be bypassed by the
        simplest possible input. The key is hashed, so length cannot matter.
        """
        monster = "+998 " + "9" * 4000
        results = [throttle.hit("t", monster, limit=2, period=60)[0]
                   for _ in range(4)]
        self.assertEqual(results, [True, True, False, False])

    def test_a_formatted_phone_number_makes_a_valid_key(self):
        """A number with spaces in it is an illegal memcached key raw."""
        key = throttle._window_key("sms.phone", "+998 90 123 45 67", 3600)
        self.assertNotIn(" ", key)
        self.assertLess(len(key), 250)

    def test_a_broken_cache_lets_traffic_through(self):
        """Fail open, deliberately.

        A limiter that refuses everyone when its counter store is unreachable
        has caused a bigger outage than the flood it was guarding against —
        and on the pay page it would turn a customer away at the counter.
        """
        with patch("core.throttle.cache.add", side_effect=OSError("no cache")):
            allowed = throttle.hit("t", "a", limit=1, period=60)[0]
        self.assertTrue(allowed)

    def test_forwarded_for_is_read_from_the_end_not_the_start(self):
        """Only the entry our own proxy appended can be trusted.

        Anyone can send an X-Forwarded-For header. If the first entry were
        used, a caller could put a new fake address in it on every request
        and never meet a limit at all.
        """
        request = type("R", (), {"META": {
            "HTTP_X_FORWARDED_FOR": "1.1.1.1, 2.2.2.2, 9.9.9.9",
            "REMOTE_ADDR": "10.0.0.1",
        }})()
        self.assertEqual(throttle.client_ip(request), "9.9.9.9")

    def test_a_forged_header_cannot_mint_new_identities(self):
        seen = set()
        for forged in ("1.1.1.1", "2.2.2.2", "3.3.3.3"):
            request = type("R", (), {"META": {
                "HTTP_X_FORWARDED_FOR": f"{forged}, 9.9.9.9",
                "REMOTE_ADDR": "10.0.0.1",
            }})()
            seen.add(throttle.client_ip(request))
        self.assertEqual(seen, {"9.9.9.9"}, "forged entries must be ignored")


@override_settings(CACHES=LOCAL_CACHE)
class LoginLimitTests(TestCase):
    def setUp(self):
        cache.clear()
        sms = patch("accounts.services.send_sms")
        sms.start()
        self.addCleanup(sms.stop)
        user = register_seller(
            phone=PHONE, full_name="Ali", business_name="Anor Cafe",
            password=PASSWORD,
        )
        user.phone_verified = True
        user.save(update_fields=["phone_verified"])

    def attempt(self, password="wrong-one"):
        return self.client.post(
            "/uz/auth/login/", {"phone": PHONE, "password": password}
        )

    def test_a_guessing_run_is_cut_off(self):
        codes = [self.attempt().status_code for _ in range(12)]
        self.assertIn(429, codes, "password guessing was never refused")

    def test_someone_who_forgot_which_password_is_not_locked_out(self):
        """Three wrong tries then the right one has to work."""
        for _ in range(3):
            self.attempt()
        response = self.attempt(PASSWORD)
        self.assertNotEqual(response.status_code, 429)
        self.assertIn("_auth_user_id", self.client.session)

    def test_reading_the_login_page_is_never_limited(self):
        """GET renders a form and costs nothing. Limiting it would refuse
        someone who simply reloaded the page a few times."""
        codes = [self.client.get("/uz/auth/login/").status_code for _ in range(40)]
        self.assertNotIn(429, codes)

    def test_the_refusal_says_how_long_to_wait(self):
        for _ in range(30):
            response = self.attempt()
            if response.status_code == 429:
                break
        self.assertEqual(response.status_code, 429)
        self.assertIn("Retry-After", response)
        self.assertGreater(int(response["Retry-After"]), 0)


@override_settings(CACHES=LOCAL_CACHE)
class SmsLimitTests(TestCase):
    """The SMS limits protect a balance, so they are the ones worth flooding."""

    def setUp(self):
        cache.clear()
        self.sms = patch("accounts.services.send_sms")
        self.sent = self.sms.start()
        self.addCleanup(self.sms.stop)

    def reset_for(self, phone):
        return self.client.post("/uz/auth/password-reset/", {"phone": phone})

    def test_walking_a_list_of_numbers_is_cut_off(self):
        """The per-number limit cannot see this; the per-address one can."""
        codes = []
        for n in range(25):
            codes.append(self.reset_for(f"+998 90 123 45 {n:02d}").status_code)
        self.assertIn(429, codes, "an SMS-draining run was never refused")

    def test_one_person_asking_twice_is_fine(self):
        for _ in range(2):
            response = self.reset_for(PHONE)
        self.assertNotEqual(response.status_code, 429)


@override_settings(CACHES=LOCAL_CACHE)
class WebhookLimitTests(TestCase):
    """The limit that could cost real money if it were wrong."""

    def setUp(self):
        cache.clear()
        sms = patch("accounts.services.send_sms")
        sms.start()
        self.addCleanup(sms.stop)
        user = register_seller(
            phone=PHONE, full_name="Ali", business_name="Anor Cafe",
            password=PASSWORD,
        )
        self.profile = SellerProfile.objects.get(user=user)
        self.profile.approve()
        save_credentials(self.profile, "payme", {"payme_id": "p", "payme_key": "k"})
        set_enabled(self.profile, "payme", True)
        self.url = f"/webhooks/payme/{self.profile.uid}/"

    def test_a_realistic_provider_burst_is_never_refused(self):
        """A provider retrying hard after an outage must get through.

        200 callbacks in a minute is far more than any of the six would send
        for one seller, and it has to pass — the alternative is a payment
        that was taken and never confirmed.
        """
        refused = 0
        for _ in range(200):
            if self.client.post(self.url, "{}",
                                content_type="application/json").status_code == 429:
                refused += 1
        self.assertEqual(refused, 0, "a genuine provider burst was throttled")

    def test_a_flood_is_eventually_cut_off(self):
        codes = []
        for _ in range(400):
            codes.append(
                self.client.post(
                    self.url, "{}", content_type="application/json"
                ).status_code
            )
        self.assertIn(429, codes, "a webhook flood was never refused")

    def test_one_sellers_flood_does_not_refuse_another(self):
        """The reason this is counted per seller and not per address."""
        for _ in range(400):
            self.client.post(self.url, "{}", content_type="application/json")

        other = register_seller(
            phone="+998 91 222 33 44", full_name="Bek",
            business_name="Bek Shop", password=PASSWORD,
        )
        other_profile = SellerProfile.objects.get(user=other)
        other_profile.approve()
        save_credentials(other_profile, "payme", {"payme_id": "p", "payme_key": "k"})
        set_enabled(other_profile, "payme", True)

        response = self.client.post(
            f"/webhooks/payme/{other_profile.uid}/", "{}",
            content_type="application/json",
        )
        self.assertNotEqual(response.status_code, 429)

    def test_a_refused_callback_answers_json_not_html(self):
        """A provider parses the body. An HTML error page would confuse it."""
        for _ in range(400):
            response = self.client.post(
                self.url, "{}", content_type="application/json"
            )
            if response.status_code == 429:
                break
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response["Content-Type"], "application/json")
        self.assertIn("Retry-After", response)


@override_settings(CACHES=LOCAL_CACHE)
class CheckoutLimitTests(TestCase):
    def setUp(self):
        cache.clear()
        sms = patch("accounts.services.send_sms")
        sms.start()
        self.addCleanup(sms.stop)
        user = register_seller(
            phone=PHONE, full_name="Ali", business_name="Anor Cafe",
            password=PASSWORD,
        )
        self.profile = SellerProfile.objects.get(user=user)
        self.profile.approve()

    def test_reading_a_pay_page_is_never_limited(self):
        """A seller's card can be tapped by a queue of customers, and the
        page itself costs nothing to serve."""
        url = f"/pay/{self.profile.uid}/"
        codes = [self.client.get(url).status_code for _ in range(60)]
        self.assertNotIn(429, codes)

    def test_a_busy_till_is_not_refused(self):
        """Twelve payments in a minute from one shop is a good afternoon,
        not an attack."""
        url = f"/pay/{self.profile.uid}/go/payme/"
        codes = [self.client.post(url, {"amount": "5000"}).status_code
                 for _ in range(12)]
        self.assertNotIn(429, codes)

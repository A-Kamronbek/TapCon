"""Sign-in must not forward a seller to somebody else's site.

``?next=`` on the login page comes from whoever wrote the link. If the view
follows it without checking, tapcon.uz becomes a springboard: the seller
sees the real domain and the real form, signs in, and is handed to a page
the attacker controls — which is then in a very good position to ask for
the password or the provider keys they just proved they have.

Signing out has the mirror problem: if a GET could do it, any page on the
web could sign a seller out of their dashboard with an <img> tag.
"""
from unittest.mock import patch

from django.test import TestCase

from accounts.services import register_seller

PHONE = "+998 90 123 45 67"
PASSWORD = "s3cretpw!x"

# Every shape of "somewhere else" a crafted link can use, including the ones
# that read as relative at a glance.
HOSTILE_TARGETS = [
    "https://evil.example/login",
    "http://evil.example/login",
    "//evil.example/login",
    "/\\evil.example/login",
    "\\\\evil.example\\login",
    "https:evil.example",
    "javascript:alert(1)",
    "data:text/html,<h1>hi",
]


class LoginRedirectTests(TestCase):
    def setUp(self):
        sms = patch("accounts.services.send_sms")
        sms.start()
        self.addCleanup(sms.stop)
        user = register_seller(
            phone=PHONE,
            full_name="Ali",
            business_name="Anor Cafe",
            password=PASSWORD,
        )
        user.phone_verified = True
        user.save(update_fields=["phone_verified"])

    def sign_in(self, query=""):
        return self.client.post(
            f"/uz/auth/login/{query}",
            {"phone": PHONE, "password": PASSWORD},
            follow=False,
        )

    def test_offsite_next_is_ignored(self):
        for target in HOSTILE_TARGETS:
            with self.subTest(target=target):
                self.client.logout()
                response = self.sign_in(f"?next={target}")
                self.assertEqual(response.status_code, 302)
                self.assertNotIn("evil.example", response["Location"])
                self.assertTrue(
                    response["Location"].startswith("/"),
                    f"{target!r} escaped to {response['Location']!r}",
                )

    def test_onsite_next_is_honoured(self):
        """The guard must not break the feature it protects."""
        response = self.sign_in("?next=/uz/merchant/transactions/")
        self.assertEqual(response["Location"], "/uz/merchant/transactions/")

    def test_no_next_goes_to_the_dashboard(self):
        response = self.sign_in()
        self.assertIn("/merchant/", response["Location"])


class LogoutMethodTests(TestCase):
    def setUp(self):
        sms = patch("accounts.services.send_sms")
        sms.start()
        self.addCleanup(sms.stop)
        user = register_seller(
            phone=PHONE,
            full_name="Ali",
            business_name="Anor Cafe",
            password=PASSWORD,
        )
        user.phone_verified = True
        user.save(update_fields=["phone_verified"])
        self.client.login(phone=PHONE, password=PASSWORD)

    def signed_in(self):
        return "_auth_user_id" in self.client.session

    def test_get_does_not_sign_out(self):
        response = self.client.get("/uz/auth/logout/")
        self.assertEqual(response.status_code, 302)  # not 405: no dead end
        self.assertTrue(self.signed_in())

    def test_post_signs_out(self):
        self.client.post("/uz/auth/logout/")
        self.assertFalse(self.signed_in())

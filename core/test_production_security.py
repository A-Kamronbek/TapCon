"""The production security settings, exercised rather than eyeballed.

`manage.py check --deploy` confirms the settings are *present*. It cannot
tell you that HTTPS redirection works behind Nginx rather than looping for
ever, that a provider's callback survives the redirect, or that a placeholder
key is refused. Those are the ways this actually goes wrong, so they are
tested.
"""
from django.core.exceptions import ImproperlyConfigured
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings

from config.settings.checks import check_production_secrets

# The security settings as prod.py sets them, applied to the test project so
# the real middleware chain runs against them.
PRODUCTION_SECURITY = dict(
    SECURE_SSL_REDIRECT=True,
    SECURE_PROXY_SSL_HEADER=("HTTP_X_FORWARDED_PROTO", "https"),
    SESSION_COOKIE_SECURE=True,
    CSRF_COOKIE_SECURE=True,
    SECURE_HSTS_SECONDS=31536000,
    SECURE_HSTS_INCLUDE_SUBDOMAINS=True,
    SECURE_HSTS_PRELOAD=True,
    SECURE_CONTENT_TYPE_NOSNIFF=True,
    X_FRAME_OPTIONS="DENY",
    SECURE_REFERRER_POLICY="same-origin",
    ALLOWED_HOSTS=["tapcon.uz", "testserver"],
)


@override_settings(**PRODUCTION_SECURITY)
class BehindNginxTests(TestCase):
    """What the middleware does with the headers a reverse proxy sends."""

    def test_a_plain_http_request_is_redirected_to_https(self):
        response = self.client.get("/uz/", secure=False)
        self.assertEqual(response.status_code, 301)
        self.assertTrue(response["Location"].startswith("https://"))

    def test_a_request_nginx_marks_as_https_is_not_redirected(self):
        """The redirect loop this guards against is the classic one.

        Nginx terminates TLS and forwards over plain HTTP. Without
        SECURE_PROXY_SSL_HEADER, Django sees http, redirects to https, Nginx
        forwards it as http again — and the site is completely unreachable,
        in a way that never appears in development.
        """
        response = self.client.get("/uz/", HTTP_X_FORWARDED_PROTO="https")
        self.assertEqual(response.status_code, 200)

    def test_a_forwarded_proto_of_http_still_redirects(self):
        response = self.client.get("/uz/", HTTP_X_FORWARDED_PROTO="http")
        self.assertEqual(response.status_code, 301)

    def test_hsts_is_sent_on_a_secure_request(self):
        response = self.client.get("/uz/", HTTP_X_FORWARDED_PROTO="https")
        header = response.get("Strict-Transport-Security", "")
        self.assertIn("max-age=31536000", header)
        self.assertIn("includeSubDomains", header)
        self.assertIn("preload", header)

    def test_the_other_headers_are_present(self):
        response = self.client.get("/uz/", HTTP_X_FORWARDED_PROTO="https")
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response["Referrer-Policy"], "same-origin")
        self.assertEqual(response["X-Frame-Options"], "DENY")

    def test_the_pay_page_can_still_be_framed_by_us(self):
        """DENY everywhere except the one view that opts out.

        The pay page carries @xframe_options_sameorigin so the merchant
        portal can preview it. A blanket DENY would blank that preview.
        """
        from unittest.mock import patch

        from accounts.services import register_seller
        from merchants.models import SellerProfile

        with patch("accounts.services.send_sms"):
            user = register_seller(
                phone="+998 90 123 45 67", full_name="Ali",
                business_name="Anor Cafe", password="s3cretpw!x",
            )
        profile = SellerProfile.objects.get(user=user)
        profile.approve()
        response = self.client.get(
            f"/pay/{profile.uid}/", HTTP_X_FORWARDED_PROTO="https"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["X-Frame-Options"], "SAMEORIGIN")


@override_settings(**PRODUCTION_SECURITY)
class WebhooksSurviveTheRedirectTests(TestCase):
    """A provider's callback must not be lost to an HTTPS redirect.

    SECURE_SSL_REDIRECT answers a plain-HTTP POST with a 301, and a 301 does
    not carry a body. If a provider is ever configured with an http:// URL by
    mistake, the confirmation is silently dropped and the payment stays
    pending. This pins what actually happens so the deploy checklist can say
    something true about it.
    """

    def test_a_callback_over_https_reaches_the_handler(self):
        response = self.client.post(
            "/webhooks/payme/nosuch/", "{}",
            content_type="application/json",
            HTTP_X_FORWARDED_PROTO="https",
        )
        # 404 because that seller does not exist — but it reached our code,
        # which is the point.
        self.assertEqual(response.status_code, 404)

    def test_a_callback_over_plain_http_is_redirected_not_processed(self):
        response = self.client.post(
            "/webhooks/payme/nosuch/", "{}",
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 301)
        self.assertTrue(response["Location"].startswith("https://"))


class ProductionSecretTests(SimpleTestCase):
    """prod.py refuses to import with a key out of .env.example."""

    REAL_SECRET = "q7Wz2fJ8xB4nR6vT1yU3sA5dG9hK0mP2cX4bN6zL8wE1rQ3tY5u"
    REAL_FERNET = "8DXbGGqXHrLmH8_Wr2cCEPuSxTlM3TzB4iRK1z0YbHw="

    def test_a_long_key_of_one_repeated_character_is_refused(self):
        """Length is not randomness. Django only warns; this refuses."""
        with self.assertRaises(ImproperlyConfigured):
            check_production_secrets("z" * 80, self.REAL_FERNET)

    def test_a_placeholder_secret_key_is_refused(self):
        for placeholder in ("change-me", "", "secret",
                            "django-insecure-abc123"):
            with self.subTest(key=placeholder):
                with self.assertRaises(ImproperlyConfigured):
                    check_production_secrets(placeholder, self.REAL_FERNET)

    def test_a_short_secret_key_is_refused(self):
        with self.assertRaises(ImproperlyConfigured):
            check_production_secrets("abc123", self.REAL_FERNET)

    def test_a_placeholder_encryption_key_is_refused(self):
        with self.assertRaises(ImproperlyConfigured):
            check_production_secrets(self.REAL_SECRET, "change-me")

    def test_the_message_says_what_losing_the_key_costs(self):
        """Whoever reads this error is about to pick a key. Tell them then."""
        with self.assertRaises(ImproperlyConfigured) as caught:
            check_production_secrets(self.REAL_SECRET, "change-me")
        message = str(caught.exception)
        self.assertIn("back it up", message.lower())
        self.assertIn("unrecoverable", message.lower())

    def test_real_keys_pass(self):
        check_production_secrets(self.REAL_SECRET, self.REAL_FERNET)


class RestoreVerificationTests(TestCase):
    """`verify_restore` has to fail on the failure it exists to catch."""

    def setUp(self):
        from unittest.mock import patch

        from accounts.services import register_seller
        from merchants.models import SellerProfile
        from payments.services import save_credentials, set_enabled

        with patch("accounts.services.send_sms"):
            user = register_seller(
                phone="+998 90 123 45 67", full_name="Ali",
                business_name="Anor Cafe", password="s3cretpw!x",
            )
        self.profile = SellerProfile.objects.get(user=user)
        self.profile.approve()
        save_credentials(self.profile, "payme",
                         {"payme_id": "id-1", "payme_key": "k-1"})
        set_enabled(self.profile, "payme", True)

    def run_command(self):
        from io import StringIO

        from django.core.management import call_command

        out = StringIO()
        call_command("verify_restore", stdout=out)
        return out.getvalue()

    def test_it_passes_on_a_healthy_database(self):
        self.assertIn("every credential decrypts", self.run_command())

    def test_it_fails_when_the_key_does_not_match(self):
        """The failure this command exists for, and it is a quiet one.

        django-encrypted-model-fields does not raise on a wrong key — it
        hands back an empty value. So a restore with the wrong key produces a
        site where every seller silently has no provider credentials, which
        looks exactly like a site where nobody has configured any yet. The
        command has to treat "everything decrypted to nothing" as a failure,
        not as an empty database.
        """
        from django.core.management import CommandError

        from payments.models import ProviderIntegration

        # Simulate what a wrong key produces: blobs that read back as empty.
        ProviderIntegration.objects.update(credentials_blob="")

        with self.assertRaises(CommandError):
            self.run_command()


class CredentialsAtRestTests(TestCase):
    """Provider keys must be unreadable in the database file itself."""

    def setUp(self):
        from unittest.mock import patch

        from accounts.services import register_seller
        from merchants.models import SellerProfile

        with patch("accounts.services.send_sms"):
            user = register_seller(
                phone="+998 90 123 45 67", full_name="Ali",
                business_name="Anor Cafe", password="s3cretpw!x",
            )
        self.profile = SellerProfile.objects.get(user=user)

    def test_a_stored_secret_never_appears_as_plain_text(self):
        from django.db import connection

        from payments.services import save_credentials

        secret = "unmistakable-secret-value-91726354"
        save_credentials(self.profile, "payme",
                         {"payme_id": "id-1", "payme_key": secret})

        with connection.cursor() as cursor:
            cursor.execute("SELECT credentials_blob FROM payments_providerintegration")
            stored = " ".join(str(row[0]) for row in cursor.fetchall())

        self.assertNotIn(secret, stored, "the key is readable in the database")
        self.assertNotIn("payme_key", stored, "the field names leak too")
        self.assertGreater(len(stored), 0, "nothing was stored at all")

    def test_it_still_reads_back_correctly(self):
        """Encrypted and unusable would be worse than not encrypted."""
        from payments.models import ProviderIntegration
        from payments.services import save_credentials

        save_credentials(self.profile, "payme",
                         {"payme_id": "id-1", "payme_key": "k-1"})
        # Read back through a fresh row, so the value really comes out of the
        # database rather than out of the instance that wrote it.
        integration = ProviderIntegration.objects.get(
            seller=self.profile, provider="payme"
        )
        self.assertEqual(
            integration.credentials,
            {"payme_id": "id-1", "payme_key": "k-1"},
        )

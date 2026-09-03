"""Provider integrations.

The security-critical property here is simple and worth stating: a stored
credential must never reach the browser again, in any form.
"""
import json
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse

from accounts.services import register_seller
from merchants.models import SellerProfile

from .models import ProviderIntegration
from .providers import PROVIDERS
from .services import (
    IntegrationError,
    connected_count,
    enabled_providers,
    integrations_for,
    save_credentials,
    set_enabled,
    set_test_mode,
)

PHONE = "+998 90 123 45 67"
PASSWORD = "s3cretpw!x"
SECRET = "super-secret-payme-key-9f2a"


class IntegrationTestCase(TestCase):
    def setUp(self):
        sms = patch("accounts.services.send_sms")
        sms.start()
        self.addCleanup(sms.stop)
        self.user = register_seller(
            phone=PHONE, full_name="Ali", business_name="Anor", password=PASSWORD
        )
        self.user.phone_verified = True
        self.user.save()
        self.profile = SellerProfile.objects.get(user=self.user)
        self.url = reverse("merchants:integrations")


class CredentialStorageTests(IntegrationTestCase):
    def test_credentials_round_trip_as_a_dict(self):
        integration = save_credentials(
            self.profile, "payme", {"payme_id": "12345", "payme_key": SECRET}
        )
        integration.refresh_from_db()
        self.assertEqual(integration.credentials["payme_id"], "12345")
        self.assertEqual(integration.credentials["payme_key"], SECRET)

    def test_the_stored_column_is_not_plain_text(self):
        save_credentials(self.profile, "payme", {"payme_id": "1", "payme_key": SECRET})
        # Read the raw column, bypassing the field's decryption.
        from django.db import connection

        with connection.cursor() as cur:
            cur.execute("SELECT credentials_blob FROM payments_providerintegration")
            raw = cur.fetchone()[0]
        self.assertNotIn(SECRET, str(raw))

    def test_blank_values_are_dropped_rather_than_stored(self):
        integration = save_credentials(
            self.profile, "payme", {"payme_id": "1", "payme_key": ""}
        )
        self.assertNotIn("payme_key", integration.credentials)

    def test_keys_outside_the_registry_are_not_stored(self):
        """gateway_kwargs() splats this dict into the tolov constructor.

        A stray key would not fail here, it would fail at payment time.
        """
        integration = save_credentials(
            self.profile, "payme", {"payme_id": "1", "nonsense": "x"}
        )
        integration.refresh_from_db()
        self.assertNotIn("nonsense", integration.credentials)
        self.assertNotIn("nonsense", integration.gateway_kwargs())

    def test_a_stray_key_already_in_the_column_is_ignored_on_read(self):
        integration = save_credentials(self.profile, "payme", {"payme_id": "1"})
        # Write past the setter, the way an older field list would have.
        ProviderIntegration.objects.filter(pk=integration.pk).update(
            credentials_blob=json.dumps({"payme_id": "1", "retired_field": "x"})
        )
        integration.refresh_from_db()
        self.assertEqual(integration.credentials, {"payme_id": "1"})

    def test_masked_value_keeps_only_the_last_four_characters(self):
        integration = save_credentials(self.profile, "payme", {"payme_key": SECRET})
        masked = integration.masked_value("payme_key")
        self.assertTrue(masked.endswith(SECRET[-4:]))
        self.assertNotIn(SECRET[:-4], masked)
        self.assertEqual(len(masked), len(SECRET))

    def test_a_short_value_is_masked_completely(self):
        integration = save_credentials(self.profile, "payme", {"payme_key": "abcd"})
        self.assertEqual(integration.masked_value("payme_key"), "••••")

    def test_completeness_follows_the_registry(self):
        integration = save_credentials(self.profile, "click", {"service_id": "1"})
        self.assertFalse(integration.is_complete)
        self.assertEqual(
            set(integration.missing_fields),
            {"merchant_id", "merchant_user_id", "secret_key"},
        )

        save_credentials(
            self.profile,
            "click",
            {
                "service_id": "1",
                "merchant_id": "2",
                "merchant_user_id": "3",
                "secret_key": "4",
            },
        )
        self.assertTrue(ProviderIntegration.objects.get(provider="click").is_complete)

    def test_gateway_kwargs_match_the_tolov_constructor(self):
        integration = save_credentials(
            self.profile, "payme", {"payme_id": "ID", "payme_key": "KEY"}
        )
        kwargs = integration.gateway_kwargs()
        self.assertEqual(
            kwargs, {"payme_id": "ID", "payme_key": "KEY", "is_test_mode": True}
        )

    def test_one_row_per_seller_and_provider(self):
        save_credentials(self.profile, "payme", {"payme_id": "1"})
        save_credentials(self.profile, "payme", {"payme_id": "2"})
        self.assertEqual(
            ProviderIntegration.objects.filter(
                seller=self.profile, provider="payme"
            ).count(),
            1,
        )


class EnableRulesTests(IntegrationTestCase):
    def test_cannot_switch_on_an_incomplete_provider(self):
        save_credentials(self.profile, "click", {"service_id": "1"})
        with self.assertRaises(IntegrationError):
            set_enabled(self.profile, "click", True)

    def test_can_switch_on_once_complete(self):
        save_credentials(self.profile, "payme", {"payme_id": "1", "payme_key": "2"})
        integration = set_enabled(self.profile, "payme", True)
        self.assertTrue(integration.is_live)

    def test_removing_a_required_field_switches_it_back_off(self):
        save_credentials(self.profile, "payme", {"payme_id": "1", "payme_key": "2"})
        set_enabled(self.profile, "payme", True)

        # Simulate a saved set that no longer has the key.
        integration = ProviderIntegration.objects.get(provider="payme")
        integration.credentials = {"payme_id": "1"}
        integration.save()
        save_credentials(self.profile, "payme", {"payme_id": "1"})

        integration.refresh_from_db()
        self.assertFalse(integration.is_enabled)

    def test_unknown_provider_is_refused(self):
        with self.assertRaises(IntegrationError):
            save_credentials(self.profile, "not-a-provider", {})
        with self.assertRaises(IntegrationError):
            set_enabled(self.profile, "not-a-provider", True)
        with self.assertRaises(IntegrationError):
            set_test_mode(self.profile, "not-a-provider", False)

    def test_connected_count(self):
        self.assertEqual(connected_count(self.profile), (0, len(PROVIDERS)))
        save_credentials(self.profile, "payme", {"payme_id": "1", "payme_key": "2"})
        set_enabled(self.profile, "payme", True)
        self.assertEqual(connected_count(self.profile), (1, len(PROVIDERS)))

    def test_enabled_providers_is_empty_until_the_seller_is_approved(self):
        save_credentials(self.profile, "payme", {"payme_id": "1", "payme_key": "2"})
        set_enabled(self.profile, "payme", True)
        self.assertEqual(enabled_providers(self.profile), [])

        self.profile.approve()
        self.assertEqual(len(enabled_providers(self.profile)), 1)

    def test_test_mode_toggles(self):
        integration = set_test_mode(self.profile, "payme", False)
        self.assertFalse(integration.is_test_mode)

    def test_every_registry_provider_gets_a_row(self):
        rows = integrations_for(self.profile)
        self.assertEqual(set(rows), set(PROVIDERS))


class IntegrationsPageTests(IntegrationTestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)

    def test_page_lists_every_provider(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        for config in PROVIDERS.values():
            self.assertContains(response, config["label"])

    def test_a_saved_secret_is_never_rendered_back(self):
        save_credentials(self.profile, "payme", {"payme_id": "12345", "payme_key": SECRET})
        body = self.client.get(self.url).content.decode("utf-8")
        self.assertNotIn(SECRET, body)
        # But the seller can see that something is stored.
        self.assertIn(SECRET[-4:], body)

    def test_each_provider_form_has_its_own_input_ids(self):
        """Click and Uzum both have service_id; Click and Paynet, merchant_id.

        With Django's default auto_id all six forms would produce
        id_service_id, so clicking Uzum's label focused Click's box.
        """
        import re

        body = self.client.get(self.url).content.decode("utf-8")
        ids = re.findall(r'\bid="(id_[^"]+)"', body)
        self.assertEqual(len(ids), len(set(ids)), "duplicate input ids on the page")

        labels = re.findall(r'<label for="(id_[^"]+)"', body)
        for target in labels:
            self.assertIn(target, ids, f"label points at a missing input: {target}")

    def test_a_saved_identifier_is_shown_so_it_can_be_checked(self):
        """Merchant IDs are not secrets, and a seller has to be able to
        compare what we hold against the provider's cabinet."""
        save_credentials(self.profile, "payme", {"payme_id": "12345", "payme_key": SECRET})
        body = self.client.get(self.url).content.decode("utf-8")
        self.assertIn('value="12345"', body)

    def test_a_provider_cannot_be_saved_half_filled(self):
        """Click has four fields. Three is not a working Click integration."""
        response = self.client.post(
            self.url,
            {"provider": "click", "action": "save", "service_id": "1",
             "merchant_id": "2", "merchant_user_id": "", "secret_key": "4"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(
            ProviderIntegration.objects.filter(provider="click").exists()
        )
        form = next(r["form"] for r in response.context["rows"] if r["key"] == "click")
        self.assertIn("merchant_user_id", form.errors)
        self.assertNotIn("service_id", form.errors)

    def test_clearing_a_stored_field_is_refused_not_silently_ignored(self):
        save_credentials(self.profile, "payme", {"payme_id": "12345", "payme_key": SECRET})
        response = self.client.post(
            self.url,
            {"provider": "payme", "action": "save", "payme_id": "", "payme_key": ""},
        )
        # The secret was never shown, so its blank box means "unchanged".
        # The identifier was shown, so an empty one is a mistake worth naming.
        form = next(r["form"] for r in response.context["rows"] if r["key"] == "payme")
        self.assertIn("payme_id", form.errors)
        self.assertNotIn("payme_key", form.errors)
        integration = ProviderIntegration.objects.get(provider="payme")
        self.assertEqual(integration.credentials["payme_id"], "12345")
        self.assertEqual(integration.credentials["payme_key"], SECRET)

    def test_an_untouched_provider_is_not_shouted_at(self):
        """Saving an empty, never-configured provider is a no-op, not an error."""
        response = self.client.post(
            self.url,
            {"provider": "payme", "action": "save", "payme_id": "", "payme_key": ""},
        )
        self.assertRedirects(response, f"{self.url}?open=payme#p-payme")

    def test_saving_through_the_page(self):
        response = self.client.post(
            self.url,
            {"provider": "payme", "action": "save",
             "payme_id": "ID-1", "payme_key": SECRET},
        )
        # Lands back on the row that was being edited, not a page of six
        # collapsed panels.
        self.assertRedirects(response, f"{self.url}?open=payme#p-payme")
        integration = ProviderIntegration.objects.get(provider="payme")
        self.assertEqual(integration.credentials["payme_key"], SECRET)

    def test_a_blank_field_keeps_the_stored_value(self):
        save_credentials(self.profile, "payme", {"payme_id": "ID-1", "payme_key": SECRET})
        self.client.post(
            self.url,
            {"provider": "payme", "action": "save",
             "payme_id": "ID-2", "payme_key": ""},
        )
        integration = ProviderIntegration.objects.get(provider="payme")
        self.assertEqual(integration.credentials["payme_id"], "ID-2")
        self.assertEqual(integration.credentials["payme_key"], SECRET)

    def test_switching_on_through_the_page(self):
        save_credentials(self.profile, "payme", {"payme_id": "1", "payme_key": "2"})
        self.client.post(self.url, {"provider": "payme", "action": "enable"})
        self.assertTrue(ProviderIntegration.objects.get(provider="payme").is_enabled)

    def test_the_edited_row_comes_back_open(self):
        response = self.client.get(self.url, {"open": "payme"})
        body = response.content.decode("utf-8")
        self.assertIn('id="p-payme"', body)
        opened = body.split('id="p-payme"')[1].split(">")[0]
        self.assertIn("open", opened)

    def test_switching_on_an_incomplete_provider_is_refused(self):
        self.client.post(self.url, {"provider": "click", "action": "enable"}, follow=True)
        self.assertFalse(
            ProviderIntegration.objects.filter(provider="click", is_enabled=True).exists()
        )

    def test_a_seller_cannot_touch_another_sellers_integration(self):
        other_user = register_seller(
            phone="+998 90 999 88 77", full_name="Bob",
            business_name="Other", password=PASSWORD,
        )
        other = SellerProfile.objects.get(user=other_user)
        save_credentials(other, "payme", {"payme_id": "OTHER", "payme_key": "OTHERKEY"})

        body = self.client.get(self.url).content.decode("utf-8")
        self.assertNotIn("OTHERKEY", body)
        self.assertNotIn("OTHER", body)

        # Saving writes to my own row, never theirs.
        self.client.post(
            self.url,
            {"provider": "payme", "action": "save", "payme_id": "MINE", "payme_key": "K"},
        )
        other_integration = ProviderIntegration.objects.get(seller=other, provider="payme")
        self.assertEqual(other_integration.credentials["payme_id"], "OTHER")

    def test_page_requires_login(self):
        self.client.logout()
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("accounts:login"), response.url)

    def test_the_accordion_needs_no_javascript(self):
        body = self.client.get(self.url).content.decode("utf-8")
        # <details> is the mechanism, so the panels open even if a script
        # never runs. JS may enhance the page, never carry it.
        self.assertIn("<details", body)


class PayPageSettingsTests(IntegrationTestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)
        self.url = reverse("merchants:payment_page")

    def test_saving_the_settings(self):
        response = self.client.post(
            self.url,
            {
                "business_name": "Anor Cafe",
                "welcome_text": "Xush kelibsiz",
                "thank_you_text": "Rahmat",
                "min_amount": 2000,
                "max_amount": 500000,
            },
        )
        self.assertRedirects(response, self.url)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.business_name, "Anor Cafe")
        self.assertEqual(self.profile.welcome_text, "Xush kelibsiz")
        self.assertEqual(int(self.profile.min_amount), 2000)

    def test_the_largest_amount_must_exceed_the_smallest(self):
        response = self.client.post(
            self.url,
            {"business_name": "Anor", "welcome_text": "", "thank_you_text": "",
             "min_amount": 5000, "max_amount": 1000},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["form"].errors)

    def test_a_negative_amount_is_refused(self):
        response = self.client.post(
            self.url,
            {"business_name": "Anor", "welcome_text": "", "thank_you_text": "",
             "min_amount": -1, "max_amount": 1000},
        )
        self.assertTrue(response.context["form"].errors)

    def test_the_preview_points_at_the_sellers_own_pay_page(self):
        response = self.client.get(self.url)
        self.assertContains(response, self.profile.get_pay_url())

    def test_the_pay_page_can_be_framed_by_us_but_not_by_others(self):
        """Without this the seller's preview is a grey box.

        SAMEORIGIN, not a blanket exemption: a payment page must stay
        unframeable from anywhere else.
        """
        response = self.client.get(self.profile.get_pay_url())
        self.assertEqual(response.headers["X-Frame-Options"], "SAMEORIGIN")

    def test_amounts_are_shown_without_tiyin(self):
        """UZS has no subunit in practice, so "1000.00" only raises questions."""
        body = self.client.get(self.url).content.decode("utf-8")
        self.assertIn('value="1000"', body)
        self.assertNotIn('value="1000.00"', body)

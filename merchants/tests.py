"""End-to-end flow tests: signup -> OTP -> dashboard, and the approval gate."""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from accounts.services import register_seller

from .models import SellerProfile, SellerStatus

User = get_user_model()
PHONE = "+998 90 123 45 67"
FIXED_CODE = "424242"
PASSWORD = "s3cretpw!x"


class FlowTestCase(TestCase):
    def setUp(self):
        sms = patch("accounts.services.send_sms")
        gen = patch("accounts.services._generate_code", return_value=FIXED_CODE)
        sms.start()
        gen.start()
        self.addCleanup(sms.stop)
        self.addCleanup(gen.stop)


class SignupFlowTests(FlowTestCase):
    def test_register_then_verify_lands_on_the_dashboard(self):
        response = self.client.post(
            reverse("accounts:register"),
            {
                "full_name": "Ali Valiyev",
                "business_name": "Anor Cafe",
                "phone": "901234567",
                "password": PASSWORD,
                "password_confirm": PASSWORD,
            },
        )
        self.assertRedirects(response, reverse("accounts:verify"))

        response = self.client.post(
            reverse("accounts:verify"), {"code": FIXED_CODE}, follow=True
        )
        self.assertRedirects(response, reverse("merchants:dashboard"))

        user = User.objects.get(phone=PHONE)
        self.assertTrue(user.phone_verified)
        self.assertContains(response, "Anor Cafe")

    def test_wrong_code_keeps_the_user_out(self):
        self.client.post(
            reverse("accounts:register"),
            {
                "full_name": "Ali",
                "business_name": "Anor Cafe",
                "phone": PHONE,
                "password": PASSWORD,
                "password_confirm": PASSWORD,
            },
        )
        response = self.client.post(reverse("accounts:verify"), {"code": "000000"})
        self.assertEqual(response.status_code, 200)
        # Stronger than "not verified": until the code is right there is no
        # account at all. Creating one on the form let anyone who knew an
        # unverified seller's number rewrite their password and rename the
        # business on their live pay page, with no code and no proof.
        self.assertFalse(User.objects.filter(phone=PHONE).exists())
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_verify_page_needs_a_pending_phone_in_the_session(self):
        response = self.client.get(reverse("accounts:verify"))
        self.assertRedirects(response, reverse("accounts:register"))

    def test_login_with_an_unverified_number_goes_back_to_the_code(self):
        register_seller(
            phone=PHONE, full_name="Ali", business_name="Anor", password=PASSWORD
        )
        response = self.client.post(
            reverse("accounts:login"), {"phone": PHONE, "password": PASSWORD}
        )
        self.assertRedirects(response, reverse("accounts:verify"))

    def test_login_works_once_verified(self):
        user = register_seller(
            phone=PHONE, full_name="Ali", business_name="Anor", password=PASSWORD
        )
        user.phone_verified = True
        user.save()
        response = self.client.post(
            reverse("accounts:login"), {"phone": "901234567", "password": PASSWORD}
        )
        self.assertRedirects(response, reverse("merchants:dashboard"))


class MerchantAccessTests(FlowTestCase):
    def setUp(self):
        super().setUp()
        self.user = register_seller(
            phone=PHONE, full_name="Ali", business_name="Anor", password=PASSWORD
        )
        self.user.phone_verified = True
        self.user.save()
        self.profile = SellerProfile.objects.get(user=self.user)

    def test_merchant_pages_require_login(self):
        for name in ("dashboard", "payment_page", "integrations", "transactions", "settings"):
            url = reverse(f"merchants:{name}")
            response = self.client.get(url)
            self.assertEqual(response.status_code, 302, msg=name)
            self.assertIn(reverse("accounts:login"), response.url, msg=name)

    def test_pending_seller_sees_the_approval_banner(self):
        # Assert on the marker class, not the copy: the page renders in Uzbek.
        self.client.force_login(self.user)
        response = self.client.get(reverse("merchants:dashboard"))
        self.assertContains(response, "approval-banner")

    def test_approved_seller_does_not(self):
        self.profile.approve()
        self.client.force_login(self.user)
        response = self.client.get(reverse("merchants:dashboard"))
        self.assertNotContains(response, "approval-banner")


class PayPageApprovalGateTests(FlowTestCase):
    def setUp(self):
        super().setUp()
        user = register_seller(
            phone=PHONE, full_name="Ali", business_name="Anor Cafe", password=PASSWORD
        )
        self.profile = SellerProfile.objects.get(user=user)

    def test_pending_seller_cannot_take_payments(self):
        response = self.client.get(self.profile.get_pay_url())
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["is_live"])

    def test_approved_seller_can(self):
        self.profile.approve()
        response = self.client.get(self.profile.get_pay_url())
        self.assertTrue(response.context["is_live"])
        self.assertContains(response, "Anor Cafe")

    def test_approve_records_who_and_when(self):
        admin = User.objects.create_superuser(phone="+998 99 999 99 99", password="pw")
        self.profile.approve(by=admin)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.status, SellerStatus.APPROVED)
        self.assertEqual(self.profile.approved_by, admin)
        self.assertIsNotNone(self.profile.approved_at)

    def test_unknown_uid_is_a_404(self):
        response = self.client.get("/pay/zzzzzz/")
        self.assertEqual(response.status_code, 404)

    def test_pay_url_carries_no_language_prefix(self):
        # This address is written onto a physical card, so it must never
        # change and must not be tied to one language.
        url = self.profile.get_pay_url()
        self.assertEqual(url, f"/pay/{self.profile.uid}/")
        for prefix in ("/uz/", "/ru/", "/en/"):
            self.assertFalse(url.startswith(prefix))


class AdminApprovalActionTests(FlowTestCase):
    """The bulk action is how sellers actually get switched on."""

    def setUp(self):
        super().setUp()
        self.admin = User.objects.create_superuser(
            phone="+998 90 000 00 00", password=PASSWORD
        )
        user = register_seller(
            phone=PHONE, full_name="Ali", business_name="Anor Cafe", password=PASSWORD
        )
        self.profile = SellerProfile.objects.get(user=user)
        self.client.force_login(self.admin)

    def test_approve_action_switches_the_seller_on(self):
        self.client.post(
            "/uz/admin/merchants/sellerprofile/",
            {"action": "approve_sellers", "_selected_action": [str(self.profile.pk)]},
        )
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.status, SellerStatus.APPROVED)
        self.assertEqual(self.profile.approved_by, self.admin)
        self.assertTrue(self.profile.is_live)

    def test_suspend_action_takes_a_seller_offline(self):
        self.profile.approve()
        self.client.post(
            "/uz/admin/merchants/sellerprofile/",
            {"action": "suspend_sellers", "_selected_action": [str(self.profile.pk)]},
        )
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.status, SellerStatus.SUSPENDED)
        self.assertFalse(self.profile.is_live)

    def test_a_plain_seller_cannot_reach_the_admin(self):
        self.client.force_login(self.profile.user)
        response = self.client.get("/uz/admin/merchants/sellerprofile/")
        self.assertNotEqual(response.status_code, 200)


class SettingsPageTests(FlowTestCase):
    def setUp(self):
        super().setUp()
        self.user = register_seller(
            phone=PHONE, full_name="Ali", business_name="Anor", password=PASSWORD
        )
        self.user.phone_verified = True
        self.user.save()
        self.profile = SellerProfile.objects.get(user=self.user)
        self.client.force_login(self.user)
        self.url = reverse("merchants:settings")

    def test_page_shows_all_three_forms(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        for key in ("profile_form", "business_form", "password_form"):
            self.assertIn(key, response.context)

    def test_saving_personal_details(self):
        self.client.post(self.url, {"form": "profile", "full_name": "Ali Valiyev"})
        self.user.refresh_from_db()
        self.assertEqual(self.user.full_name, "Ali Valiyev")

    def test_the_name_cannot_be_erased(self):
        """Registration demands a name, so settings must not let it go.

        The model allows blank (a command-line superuser has none), which is
        why this is enforced on the form.
        """
        self.user.full_name = "Ali Valiyev"
        self.user.save()
        response = self.client.post(self.url, {"form": "profile", "full_name": ""})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["profile_form"].errors)
        self.user.refresh_from_db()
        self.assertEqual(self.user.full_name, "Ali Valiyev")

    def test_saving_business_details_normalises_the_contact_phone(self):
        self.client.post(
            self.url,
            {
                "form": "business",
                "business_name": "Anor Cafe",
                "contact_phone": "901112233",
                "address": "Tashkent, Chilanzar 5",
            },
        )
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.business_name, "Anor Cafe")
        self.assertEqual(self.profile.contact_phone, "+998 90 111 22 33")
        self.assertEqual(self.profile.address, "Tashkent, Chilanzar 5")

    def test_contact_phone_may_be_left_blank(self):
        response = self.client.post(
            self.url,
            {"form": "business", "business_name": "Anor", "contact_phone": "", "address": ""},
        )
        self.assertRedirects(response, self.url)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.contact_phone, "")

    def test_a_bad_contact_phone_is_rejected(self):
        response = self.client.post(
            self.url,
            {"form": "business", "business_name": "Anor", "contact_phone": "12345", "address": ""},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["business_form"].errors)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.contact_phone, "")

    def test_a_seller_cannot_edit_their_own_approval_status(self):
        self.client.post(
            self.url,
            {
                "form": "business",
                "business_name": "Anor",
                "contact_phone": "",
                "address": "",
                "status": SellerStatus.APPROVED,
                "uid": "hacked",
            },
        )
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.status, SellerStatus.PENDING)
        self.assertNotEqual(self.profile.uid, "hacked")

    def test_changing_the_password_keeps_the_seller_signed_in(self):
        new_password = "an0ther-s3cret!"
        response = self.client.post(
            self.url,
            {
                "form": "password",
                "old_password": PASSWORD,
                "new_password1": new_password,
                "new_password2": new_password,
            },
        )
        self.assertRedirects(response, self.url)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(new_password))
        # Still signed in: the session hash was refreshed.
        self.assertEqual(self.client.get(self.url).status_code, 200)

    def test_the_wrong_current_password_changes_nothing(self):
        response = self.client.post(
            self.url,
            {
                "form": "password",
                "old_password": "not-my-password",
                "new_password1": "an0ther-s3cret!",
                "new_password2": "an0ther-s3cret!",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(PASSWORD))

    def test_settings_requires_login(self):
        self.client.logout()
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("accounts:login"), response.url)

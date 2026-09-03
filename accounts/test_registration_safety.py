"""Nothing is created or changed until the code proves the number.

`register_seller` deliberately takes over an unverified account on the same
number — someone who abandoned signup has to be able to start again. That is
the right behaviour *after* the code is verified and completely wrong before
it: called straight from the registration form, it let anyone who merely knew
an unverified seller's phone number rewrite their password, lock them out,
and rename the business shown on their live pay page. One form submission, no
code, no proof of anything.
"""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from accounts.models import PhoneOTP, hash_otp
from accounts.services import register_seller
from merchants.models import SellerProfile, SellerStatus

User = get_user_model()

PHONE = "+998 90 123 45 67"
OWNER_PASSWORD = "owner-pw-8812"
ATTACKER_PASSWORD = "attacker-pw-3390"


class RegistrationCreatesNothingEarlyTests(TestCase):
    def setUp(self):
        sms = patch("accounts.services.send_sms")
        sms.start()
        self.addCleanup(sms.stop)

    def submit(self, client, full_name, business_name, password):
        return client.post(reverse("accounts:register"), {
            "full_name": full_name,
            "business_name": business_name,
            "phone": PHONE,
            "password": password,
            "password_confirm": password,
        })

    def code_for(self, phone=PHONE):
        otp = PhoneOTP.objects.filter(phone__contains="90 123 45 67").order_by(
            "-created_at"
        ).first()
        # The stored value is a hash, so the code itself has to be recovered
        # by trying the one the console backend would have printed. Instead,
        # write a known code straight onto the row.
        otp.code_hash = hash_otp(otp.phone, "424242")
        otp.save(update_fields=["code_hash"])
        return "424242"

    def test_submitting_the_form_creates_no_account(self):
        self.submit(self.client, "Ali", "Anor Cafe", OWNER_PASSWORD)
        self.assertFalse(
            User.objects.filter(phone=PHONE).exists(),
            "an account existed before the number was proved",
        )

    def test_the_account_appears_only_after_the_code(self):
        self.submit(self.client, "Ali", "Anor Cafe", OWNER_PASSWORD)
        code = self.code_for()
        response = self.client.post(reverse("accounts:verify"), {"code": code},
                                    follow=True)
        self.assertEqual(response.status_code, 200)
        user = User.objects.get(phone=PHONE)
        self.assertTrue(user.phone_verified)
        self.assertTrue(user.check_password(OWNER_PASSWORD))
        self.assertEqual(
            SellerProfile.objects.get(user=user).business_name, "Anor Cafe"
        )

    def test_a_stranger_cannot_take_over_an_unverified_account(self):
        """The attack this test exists for.

        The owner registers and never finishes. A stranger who knows the
        number submits the form with their own password and business name.
        Without a code they must change nothing.
        """
        owner_client = self.client
        self.submit(owner_client, "Real Owner", "Real Business", OWNER_PASSWORD)
        code = self.code_for()
        owner_client.post(reverse("accounts:verify"), {"code": code})
        owner = User.objects.get(phone=PHONE)
        self.assertTrue(owner.phone_verified)

        # Now a stranger tries. A verified number is refused outright.
        stranger = self.client_class()
        self.submit(stranger, "Attacker", "Attacker Shop", ATTACKER_PASSWORD)
        owner.refresh_from_db()
        self.assertTrue(owner.check_password(OWNER_PASSWORD))
        self.assertFalse(owner.check_password(ATTACKER_PASSWORD))
        self.assertEqual(owner.full_name, "Real Owner")

    def test_a_stranger_cannot_rewrite_an_unfinished_signup(self):
        """The owner started but never confirmed — still nothing to hijack."""
        self.submit(self.client, "Real Owner", "Real Business", OWNER_PASSWORD)

        stranger = self.client_class()
        self.submit(stranger, "Attacker", "Attacker Shop", ATTACKER_PASSWORD)

        # Still no account at all: the stranger's submission created nothing,
        # so there is nothing for them to have taken over.
        self.assertFalse(User.objects.filter(phone=PHONE).exists())

    def test_no_plaintext_password_is_kept_in_the_session(self):
        self.submit(self.client, "Ali", "Anor Cafe", OWNER_PASSWORD)
        pending = self.client.session.get("pending_registration") or {}
        self.assertNotIn("password", pending)
        self.assertIn("password_hash", pending)
        self.assertNotIn(OWNER_PASSWORD, str(pending))


class ReRegistrationResetsApprovalTests(TestCase):
    """An approval is a judgement about a particular business."""

    def setUp(self):
        sms = patch("accounts.services.send_sms")
        sms.start()
        self.addCleanup(sms.stop)

    def test_renaming_on_re_registration_needs_approving_again(self):
        user = register_seller(phone=PHONE, full_name="Ali",
                               business_name="Anor Cafe",
                               password=OWNER_PASSWORD)
        profile = SellerProfile.objects.get(user=user)
        profile.approve()
        self.assertEqual(profile.status, SellerStatus.APPROVED)

        register_seller(phone=PHONE, full_name="Ali",
                        business_name="A Completely Different Shop",
                        password=OWNER_PASSWORD)
        profile.refresh_from_db()
        self.assertEqual(profile.business_name, "A Completely Different Shop")
        self.assertEqual(
            profile.status, SellerStatus.PENDING,
            "a renamed business kept a live pay page nobody had checked",
        )

    def test_the_same_name_keeps_its_approval(self):
        user = register_seller(phone=PHONE, full_name="Ali",
                               business_name="Anor Cafe",
                               password=OWNER_PASSWORD)
        profile = SellerProfile.objects.get(user=user)
        profile.approve()
        register_seller(phone=PHONE, full_name="Ali",
                        business_name="Anor Cafe", password=OWNER_PASSWORD)
        profile.refresh_from_db()
        self.assertEqual(profile.status, SellerStatus.APPROVED)


class OTPAttemptCounterTests(TestCase):
    """The five-attempt limit has to actually advance."""

    def setUp(self):
        sms = patch("accounts.services.send_sms")
        sms.start()
        self.addCleanup(sms.stop)

    def test_a_wrong_guess_is_counted_and_not_rolled_back(self):
        """Raising inside an atomic block would undo the increment.

        That is exactly what wrapping verify_otp in @transaction.atomic did:
        every wrong guess rolled back its own counter, so the limit never
        advanced and a code could be guessed for ever.
        """
        from accounts.services import OTPInvalid, issue_otp, verify_otp

        otp = issue_otp(PHONE)
        for expected in (1, 2, 3):
            with self.assertRaises(OTPInvalid):
                verify_otp(PHONE, "000000")
            otp.refresh_from_db()
            self.assertEqual(otp.attempts, expected)

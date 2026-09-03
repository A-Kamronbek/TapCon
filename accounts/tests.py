from datetime import timedelta
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from merchants.models import SellerProfile, SellerStatus, generate_uid

from .models import OTPPurpose, PhoneOTP, hash_otp, normalize_uz_phone
from .services import OTPInvalid, OTPThrottled, issue_otp, register_seller, verify_otp

User = get_user_model()
PHONE = "+998 90 123 45 67"
FIXED_CODE = "424242"


class PhoneNormalisationTests(TestCase):
    def test_accepts_the_shapes_people_actually_type(self):
        for raw in [
            "901234567",
            "+998901234567",
            "998901234567",
            "+998 90 123 45 67",
            "+998-90-123-45-67",
            "(90) 123 45 67",
        ]:
            self.assertEqual(normalize_uz_phone(raw), PHONE, msg=raw)

    def test_leaves_unrecognisable_input_alone(self):
        self.assertEqual(normalize_uz_phone("12345"), "12345")
        self.assertIsNone(normalize_uz_phone(None))

    def test_user_is_normalised_on_save(self):
        user = User.objects.create_user(phone="901234567", password="pw")
        self.assertEqual(user.phone, PHONE)


class OTPTestCase(TestCase):
    """Pins the code generator and stubs SMS so tests never send anything."""

    def setUp(self):
        sms = patch("accounts.services.send_sms")
        gen = patch("accounts.services._generate_code", return_value=FIXED_CODE)
        self.send_sms = sms.start()
        gen.start()
        self.addCleanup(sms.stop)
        self.addCleanup(gen.stop)


class OTPTests(OTPTestCase):
    def test_issue_stores_a_hash_not_the_code(self):
        otp = issue_otp(PHONE)
        self.assertEqual(otp.code_hash, hash_otp(PHONE, FIXED_CODE))
        self.assertNotIn(FIXED_CODE, otp.code_hash)
        # The plain code leaves the system only inside the SMS.
        self.assertIn(FIXED_CODE, self.send_sms.call_args.args[1])

    def test_verify_consumes_the_code(self):
        otp = issue_otp(PHONE)
        verify_otp(PHONE, FIXED_CODE)
        otp.refresh_from_db()
        self.assertIsNotNone(otp.consumed_at)

    def test_a_code_cannot_be_reused(self):
        issue_otp(PHONE)
        verify_otp(PHONE, FIXED_CODE)
        with self.assertRaises(OTPInvalid):
            verify_otp(PHONE, FIXED_CODE)

    def test_expired_code_is_rejected(self):
        otp = issue_otp(PHONE)
        PhoneOTP.objects.filter(pk=otp.pk).update(
            expires_at=timezone.now() - timedelta(seconds=1)
        )
        with self.assertRaises(OTPInvalid):
            verify_otp(PHONE, FIXED_CODE)

    def test_wrong_code_burns_an_attempt_then_locks_out(self):
        otp = issue_otp(PHONE)
        for _ in range(settings.OTP_MAX_ATTEMPTS):
            with self.assertRaises(OTPInvalid):
                verify_otp(PHONE, "111111")

        otp.refresh_from_db()
        self.assertEqual(otp.attempts, settings.OTP_MAX_ATTEMPTS)
        # Even the right code is dead once the attempts are spent.
        with self.assertRaises(OTPInvalid):
            verify_otp(PHONE, FIXED_CODE)

    def test_resend_is_rate_limited(self):
        issue_otp(PHONE)
        with self.assertRaises(OTPThrottled) as ctx:
            issue_otp(PHONE)
        self.assertGreater(ctx.exception.retry_after, 0)

    @override_settings(OTP_RESEND_COOLDOWN_SECONDS=0, OTP_MAX_PER_HOUR=3)
    def test_hourly_cap(self):
        for _ in range(3):
            issue_otp(PHONE)
        with self.assertRaises(OTPThrottled):
            issue_otp(PHONE)

    @override_settings(OTP_RESEND_COOLDOWN_SECONDS=0)
    def test_issuing_a_new_code_kills_the_old_one(self):
        first = issue_otp(PHONE)
        issue_otp(PHONE)
        first.refresh_from_db()
        self.assertIsNotNone(first.consumed_at)

    def test_purposes_do_not_cross(self):
        issue_otp(PHONE, OTPPurpose.REGISTER)
        with self.assertRaises(OTPInvalid):
            verify_otp(PHONE, FIXED_CODE, OTPPurpose.RESET)

    def test_verify_normalises_the_phone_it_is_given(self):
        issue_otp(PHONE)
        verify_otp("901234567", FIXED_CODE)  # same number, different spelling


class RegistrationTests(OTPTestCase):
    def test_registration_creates_a_pending_seller(self):
        user = register_seller(
            phone="901234567", full_name="Ali", business_name="Anor Cafe", password="s3cretpw!x"
        )
        self.assertEqual(user.phone, PHONE)
        self.assertTrue(user.is_seller)
        self.assertFalse(user.phone_verified)

        profile = SellerProfile.objects.get(user=user)
        self.assertEqual(profile.status, SellerStatus.PENDING)
        self.assertFalse(profile.is_live)
        self.assertEqual(len(profile.uid), 6)

    def test_an_unverified_signup_can_be_retried(self):
        register_seller(phone=PHONE, full_name="Ali", business_name="First", password="s3cretpw!x")
        register_seller(phone=PHONE, full_name="Ali", business_name="Second", password="s3cretpw!x")
        self.assertEqual(User.objects.filter(phone=PHONE).count(), 1)
        self.assertEqual(SellerProfile.objects.get(user__phone=PHONE).business_name, "Second")

    def test_a_verified_number_cannot_be_taken_over(self):
        user = register_seller(
            phone=PHONE, full_name="Ali", business_name="First", password="s3cretpw!x"
        )
        user.phone_verified = True
        user.save()
        with self.assertRaises(ValueError):
            register_seller(
                phone=PHONE, full_name="Bob", business_name="Second", password="s3cretpw!x"
            )


class SellerUidTests(TestCase):
    def test_uid_avoids_confusable_characters(self):
        for _ in range(200):
            self.assertFalse(set(generate_uid()) & set("01loi"))

    def test_uid_is_assigned_and_unique(self):
        uids = set()
        for i in range(20):
            user = User.objects.create_user(phone=f"9012345{i:02d}", password="pw")
            uids.add(SellerProfile.objects.create(user=user, business_name=f"B{i}").uid)
        self.assertEqual(len(uids), 20)


class UzbekLanguageTests(TestCase):
    """Uzbek is the default language, so nothing a seller sees may be English."""

    def test_uzbek_is_the_default(self):
        self.assertEqual(settings.LANGUAGE_CODE, "uz")

    def test_root_redirects_to_uzbek(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/uz/")

    def test_password_validator_messages_are_translated(self):
        from django.core.exceptions import ValidationError
        from django.contrib.auth.password_validation import validate_password
        from django.utils import translation

        # Django ships no Uzbek translation for these, so we supply our own.
        cases = ["short", "password", "12345678901"]
        with translation.override("uz"):
            for raw in cases:
                try:
                    validate_password(raw)
                except ValidationError as exc:
                    for message in exc.messages:
                        self.assertNotIn("This password", message, msg=raw)
                        self.assertNotIn("characters long", message.replace(
                            "belgidan", ""), msg=raw)
                else:
                    self.fail(f"{raw!r} should have been rejected")

    def test_register_form_errors_come_back_in_uzbek(self):
        response = self.client.post(
            "/uz/auth/register/",
            {
                "full_name": "Ali",
                "business_name": "Anor",
                "phone": "901234567",
                "password": "12345",
                "password_confirm": "54321",
            },
        )
        self.assertEqual(response.status_code, 200)
        body = response.content.decode("utf-8")
        self.assertNotIn("This password is too", body)
        self.assertNotIn("The two password fields", body)
        self.assertIn("Parollar mos kelmadi.", body)


class TranslationCatalogueTests(TestCase):
    """The catalogues must stay complete — a gap ships English to a seller."""

    def test_no_untranslated_or_fuzzy_entries(self):
        import polib
        from django.conf import settings as s

        for lang in ("uz", "ru"):
            path = s.BASE_DIR / "locale" / lang / "LC_MESSAGES" / "django.po"
            po = polib.pofile(str(path), encoding="utf-8")

            fuzzy = [e.msgid for e in po if "fuzzy" in e.flags and not e.obsolete]
            self.assertEqual(fuzzy, [], msg=f"{lang} has fuzzy (guessed) entries")

            missing = []
            for entry in po:
                if entry.obsolete or not entry.msgid:
                    continue
                if entry.msgid_plural:
                    if not any(entry.msgstr_plural.values()):
                        missing.append(entry.msgid)
                elif not entry.msgstr:
                    missing.append(entry.msgid)
            self.assertEqual(missing, [], msg=f"{lang} has untranslated strings")

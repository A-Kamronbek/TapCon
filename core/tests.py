"""Card orders, contact messages, notifications and the money filter."""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from accounts.services import register_seller
from merchants.models import SellerProfile

from .models import CardOrder, ContactMessage, Notification, NotificationLevel, OrderStatus
from .services import notify, unread_count
from .templatetags.humanize_uzs import uzs

User = get_user_model()
PASSWORD = "s3cretpw!x"


NBSP = "\u00a0"


class MoneyFilterTests(TestCase):
    def test_thousands_are_grouped_with_a_non_breaking_space(self):
        # Non-breaking on purpose: an amount must never wrap mid-number.
        self.assertEqual(uzs(100000), f"100{NBSP}000")
        self.assertEqual(uzs(1250000), f"1{NBSP}250{NBSP}000")
        self.assertEqual(uzs(999), "999")

    def test_whole_amounts_lose_the_decimal_tail(self):
        from decimal import Decimal

        self.assertEqual(uzs(Decimal("100000.00")), f"100{NBSP}000")
        self.assertEqual(uzs(Decimal("1500.50")), f"1{NBSP}500,50")

    def test_junk_is_returned_unchanged(self):
        self.assertEqual(uzs(""), "")
        self.assertEqual(uzs(None), "")
        self.assertEqual(uzs("abc"), "abc")


@patch("core.services.send_message")
class CardOrderTests(TestCase):
    url_name = "core:order"

    def _post(self, **overrides):
        data = {
            "full_name": "Ali Valiyev",
            "phone": "901234567",
            "address": "Tashkent, Chilanzar 5",
            "quantity": 3,
            "comment": "A cafe",
            **overrides,
        }
        # The Telegram call is deferred with transaction.on_commit, which a
        # TestCase would otherwise roll back before it ever runs.
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(reverse(self.url_name), data)

    def test_an_order_is_saved_and_normalised(self, send_message):
        response = self._post()
        self.assertRedirects(response, reverse("core:order_done"))

        order = CardOrder.objects.get()
        self.assertEqual(order.full_name, "Ali Valiyev")
        self.assertEqual(order.phone, "+998 90 123 45 67")
        self.assertEqual(order.quantity, 3)
        self.assertEqual(order.status, OrderStatus.NEW)

    def test_total_price_uses_the_configured_card_price(self, send_message):
        self._post(quantity=4)
        with self.settings(CARD_PRICE_UZS=100_000):
            self.assertEqual(CardOrder.objects.get().total_price, 400_000)

    def test_telegram_is_told_about_the_order(self, send_message):
        self._post()
        send_message.assert_called_once()
        text = send_message.call_args.args[0]
        self.assertIn("Ali Valiyev", text)
        self.assertIn("+998 90 123 45 67", text)
        self.assertIn("3", text)

    def test_a_bad_phone_is_rejected(self, send_message):
        response = self._post(phone="12345")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(CardOrder.objects.exists())

    def test_quantity_must_be_sensible(self, send_message):
        for bad in (0, 501):
            self._post(quantity=bad)
        self.assertFalse(CardOrder.objects.exists())

    def test_a_missing_telegram_token_does_not_break_the_order(self, send_message):
        # The real send_message returns False when unconfigured; make sure a
        # failure to notify never costs us the order itself.
        send_message.return_value = False
        response = self._post()
        self.assertRedirects(response, reverse("core:order_done"))
        self.assertTrue(CardOrder.objects.exists())


@patch("core.services.send_message")
class ContactMessageTests(TestCase):
    def test_a_message_is_saved_and_confirmed(self, send_message):
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                reverse("core:contact"),
                {"full_name": "Ali", "phone": "901234567", "message": "Salom"},
                follow=True,
            )
        self.assertEqual(response.status_code, 200)
        contact = ContactMessage.objects.get()
        self.assertEqual(contact.phone, "+998 90 123 45 67")
        self.assertFalse(contact.is_handled)
        send_message.assert_called_once()

    def test_html_in_a_message_cannot_break_the_telegram_payload(self, send_message):
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(
                reverse("core:contact"),
                {
                    "full_name": "<b>Ali</b>",
                    "phone": "901234567",
                    "message": "<script>x</script>",
                },
            )
        text = send_message.call_args.args[0]
        self.assertNotIn("<script>", text)
        self.assertIn("&lt;script&gt;", text)


class TelegramConfigTests(TestCase):
    def test_send_is_skipped_when_no_token_is_set(self):
        from .telegram import is_configured, send_message

        with self.settings(TELEGRAM_BOT_TOKEN="", TELEGRAM_CHAT_ID=""):
            self.assertFalse(is_configured())
            self.assertFalse(send_message("hello"))


class NotificationTestCase(TestCase):
    def setUp(self):
        sms = patch("accounts.services.send_sms")
        sms.start()
        self.addCleanup(sms.stop)
        self.user = register_seller(
            phone="+998 90 123 45 67", full_name="Ali", business_name="Anor",
            password=PASSWORD,
        )
        self.user.phone_verified = True
        self.user.save()
        self.profile = SellerProfile.objects.get(user=self.user)


class NotificationTests(NotificationTestCase):
    def test_approving_a_seller_notifies_them(self):
        self.profile.approve()
        note = Notification.objects.get(user=self.user)
        self.assertEqual(note.level, NotificationLevel.SUCCESS)
        self.assertFalse(note.is_read)
        self.assertEqual(note.url, reverse("merchants:dashboard"))

    def test_approving_twice_does_not_notify_twice(self):
        self.profile.approve()
        self.profile.approve()
        self.assertEqual(Notification.objects.filter(user=self.user).count(), 1)

    def test_suspending_a_live_seller_notifies_them(self):
        self.profile.approve()
        self.profile.suspend()
        self.assertEqual(Notification.objects.filter(user=self.user).count(), 2)
        self.assertEqual(
            Notification.objects.first().level, NotificationLevel.WARNING
        )

    def test_suspending_a_pending_seller_says_nothing(self):
        self.profile.suspend()
        self.assertFalse(Notification.objects.exists())

    def test_unread_count(self):
        self.assertEqual(unread_count(self.user), 0)
        notify(self.user, title="One")
        notify(self.user, title="Two")
        self.assertEqual(unread_count(self.user), 2)

    def test_the_bell_shows_the_count(self):
        notify(self.user, title="Something happened")
        self.client.force_login(self.user)
        response = self.client.get(reverse("merchants:dashboard"))
        self.assertEqual(response.context["unread_notifications"], 1)
        self.assertContains(response, "bell-count")

    def test_marking_all_read_clears_the_bell(self):
        notify(self.user, title="One")
        self.client.force_login(self.user)
        self.client.post(reverse("core:notifications_read"))
        self.assertEqual(unread_count(self.user), 0)

    def test_a_seller_only_sees_their_own_notifications(self):
        other = User.objects.create_user(phone="+998 90 999 88 77", password=PASSWORD)
        notify(other, title="Not for you")
        notify(self.user, title="For you")

        self.client.force_login(self.user)
        response = self.client.get(reverse("core:notifications"))
        self.assertContains(response, "For you")
        self.assertNotContains(response, "Not for you")

    def test_notifications_require_login(self):
        response = self.client.get(reverse("core:notifications"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("accounts:login"), response.url)

    def test_a_get_marks_nothing_read(self):
        """Only a POST changes state — anything prefetches links.

        It answers with a redirect rather than 405: this URL can be reloaded
        or gone back to, and a 405 in an address bar is a dead end with no
        page and nothing to act on.
        """
        notify(self.user, title="Still unread")
        self.client.force_login(self.user)

        response = self.client.get(reverse("core:notifications_read"))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("core:notifications"))
        self.assertEqual(
            Notification.objects.filter(user=self.user, is_read=False).count(), 1
        )


class SingleNotificationReadTests(NotificationTestCase):
    """Clicking one message must mark that message read.

    Before this existed only "mark all read" worked, so a seller could never
    clear one item and keep the rest.
    """

    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)
        self.one = notify(self.user, title="One")
        self.two = notify(self.user, title="Two")
        self.url = reverse("core:notification_read", args=[self.one.pk])

    def test_reading_one_leaves_the_others_alone(self):
        self.client.post(self.url)
        self.one.refresh_from_db()
        self.two.refresh_from_db()
        self.assertTrue(self.one.is_read)
        self.assertFalse(self.two.is_read)
        self.assertEqual(unread_count(self.user), 1)

    def test_it_goes_where_the_notification_points(self):
        target = reverse("merchants:dashboard")
        item = notify(self.user, title="Go", url=target)
        response = self.client.post(reverse("core:notification_read", args=[item.pk]))
        self.assertRedirects(response, target)

    def test_without_a_target_it_comes_back_where_you_were(self):
        here = reverse("core:notifications")
        response = self.client.post(self.url, {"next": here})
        self.assertRedirects(response, here)

    def test_an_off_site_next_is_ignored(self):
        response = self.client.post(self.url, {"next": "https://evil.example/"})
        self.assertRedirects(response, reverse("core:notifications"))

    def test_the_row_is_a_form_not_a_bare_link(self):
        """A state change on GET would fire on any link prefetch."""
        body = self.client.get(reverse("core:notifications")).content.decode("utf-8")
        self.assertIn(f'action="{self.url}"', body)
        self.assertIn('class="notif-row"', body)

    def test_javascript_gets_the_new_count_back(self):
        response = self.client.post(self.url, headers={"x-requested-with": "XMLHttpRequest"})
        self.assertEqual(response.json()["unread"], 1)

    def test_a_seller_cannot_read_someone_elses_notification(self):
        other = User.objects.create_user(phone="+998 90 777 66 55", password=PASSWORD)
        theirs = notify(other, title="Not yours")
        response = self.client.post(
            reverse("core:notification_read", args=[theirs.pk])
        )
        self.assertEqual(response.status_code, 404)
        theirs.refresh_from_db()
        self.assertFalse(theirs.is_read)

    def test_a_get_marks_nothing_read(self):
        """Same rule as marking everything read, and the same reason."""
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("core:notifications"))
        self.one.refresh_from_db()
        self.assertFalse(self.one.is_read)

    def test_reading_one_requires_login(self):
        self.client.logout()
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("accounts:login"), response.url)


class MarketingPageTests(TestCase):
    def test_every_marketing_page_answers(self):
        for name in (
            "home", "how_it_works", "for_business", "pricing", "about",
            "faq", "privacy", "terms", "order", "order_done", "contact",
        ):
            with self.subTest(page=name):
                self.assertEqual(self.client.get(reverse(f"core:{name}")).status_code, 200)

    def test_the_card_price_is_shown_with_a_thousands_separator(self):
        with self.settings(CARD_PRICE_UZS=100_000):
            response = self.client.get(reverse("core:pricing"))
        self.assertContains(response, f"100{NBSP}000")

    def test_the_nav_links_go_to_real_pages(self):
        response = self.client.get(reverse("core:home"))
        body = response.content.decode("utf-8")
        for name in ("how_it_works", "for_business", "pricing", "faq", "contact"):
            self.assertIn(reverse(f"core:{name}"), body, msg=name)

    def test_the_mobile_menu_needs_no_javascript(self):
        # The drawer opens from a checkbox and its labels, so it keeps working
        # if a script fails to load. JS may enhance it, never carry it.
        body = self.client.get(reverse("core:home")).content.decode("utf-8")
        self.assertIn('id="nav-toggle"', body)
        self.assertIn('for="nav-toggle"', body)


class FormErrorMarkupTests(TestCase):
    """A rejected field must look rejected, not only read as rejected."""

    def test_an_invalid_field_gets_the_error_class(self):
        response = self.client.post(
            reverse("core:order"),
            {
                "full_name": "Ali",
                "phone": "12345",  # not a valid Uzbek number
                "address": "Tashkent",
                "quantity": 1,
                "comment": "",
            },
        )
        body = response.content.decode("utf-8")
        self.assertIn("is-invalid", body)
        # The class lands on the phone input, not on every field.
        self.assertEqual(body.count("is-invalid"), 1)

    def test_a_clean_form_carries_no_error_class(self):
        body = self.client.get(reverse("core:order")).content.decode("utf-8")
        self.assertNotIn("is-invalid", body)


class HeroAndLogoAssetTests(TestCase):
    """The image slots must point at files that actually exist."""

    def test_referenced_images_are_on_disk(self):
        """Every {% static 'img/...' %} in every template must resolve.

        Scanned rather than listed, so renaming or adding artwork cannot
        leave a broken <img> behind unnoticed.
        """
        import re

        from django.conf import settings as s

        pattern = re.compile(r"""static\s+['"](img/[^'"]+)['"]""")
        found = set()
        for path in (s.BASE_DIR / "templates").rglob("*.html"):
            found.update(pattern.findall(path.read_text(encoding="utf-8")))

        self.assertIn("img/hero-card.png", found)
        for rel in sorted(found):
            with self.subTest(asset=rel):
                self.assertTrue((s.BASE_DIR / "static" / rel).exists(), rel)

    def test_the_home_page_uses_the_image_slots(self):
        body = self.client.get(reverse("core:home")).content.decode("utf-8")
        self.assertIn("img/hero-card.png", body)
        self.assertIn("img/providers/click.png", body)
        # The old CSS-drawn card is gone.
        self.assertNotIn("card-visual", body)


class MobileNavStackingTests(TestCase):
    """Guards the bug where the dimming scrim covered the open menu.

    The scrim must sit above the page but below the header bar and the
    drawer, otherwise it greys out the menu and swallows every tap on it.
    """

    def _nav_css(self):
        from django.conf import settings as s

        return (s.BASE_DIR / "static" / "css" / "nav.css").read_text(encoding="utf-8")

    def _z(self, css, selector):
        import re

        # The z-index declared in the last rule whose selector matches exactly.
        pattern = re.compile(
            re.escape(selector) + r"\s*\{[^}]*?z-index:\s*(\d+)", re.S
        )
        found = pattern.findall(css)
        self.assertTrue(found, f"no z-index found for {selector}")
        return int(found[-1])

    def test_scrim_sits_below_the_header_bar_and_the_drawer(self):
        css = self._nav_css()
        scrim = self._z(css, ".drawer-scrim")
        bar = self._z(css, ".nav-bar")
        drawer = self._z(css, ".site-nav")

        self.assertLess(scrim, bar, "scrim would cover the logo, bell and burger")
        self.assertLess(scrim, drawer, "scrim would cover the open menu")

    def test_merchant_drawer_also_clears_the_scrim(self):
        from django.conf import settings as s

        nav = self._nav_css()
        layouts = (s.BASE_DIR / "static" / "css" / "layouts.css").read_text(encoding="utf-8")
        scrim = self._z(nav, ".drawer-scrim")
        self.assertLess(scrim, self._z(layouts, ".merchant-topbar"))
        self.assertLess(scrim, self._z(layouts, ".merchant-sidebar"))

    def test_both_drawers_open_without_javascript(self):
        # Checkbox driven: the markup must carry the toggles and their labels.
        home = self.client.get(reverse("core:home")).content.decode("utf-8")
        self.assertIn('id="nav-toggle"', home)
        self.assertIn('for="nav-toggle"', home)
        self.assertIn("drawer-scrim", home)

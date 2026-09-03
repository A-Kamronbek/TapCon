from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import PasswordChangeForm
from django.utils.translation import gettext_lazy as _

from accounts.models import normalize_uz_phone

from .models import SellerProfile

User = get_user_model()


class ProfileForm(forms.ModelForm):
    """Who the seller is. The login phone is not editable here — changing it
    would need a new OTP round, which is a separate flow."""

    class Meta:
        model = User
        fields = ("full_name",)
        widgets = {
            "full_name": forms.TextInput(attrs={"class": "input", "autocomplete": "name"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # The model allows a blank name, because a superuser created from the
        # command line has none. A seller is asked for it at registration, so
        # letting them erase it here would leave a nameless seller in admin
        # and in the Telegram notifications.
        self.fields["full_name"].required = True


class BusinessForm(forms.ModelForm):
    class Meta:
        model = SellerProfile
        fields = ("business_name", "contact_phone", "address")
        widgets = {
            "business_name": forms.TextInput(
                attrs={"class": "input", "autocomplete": "organization"}
            ),
            "contact_phone": forms.TextInput(
                attrs={"class": "input", "placeholder": "+998 90 123 45 67", "inputmode": "tel"}
            ),
            "address": forms.TextInput(attrs={"class": "input"}),
        }
        help_texts = {
            "business_name": _("Shown to customers on your payment page."),
            "contact_phone": _("Optional. Shown to customers who need help."),
        }

    def clean_contact_phone(self):
        value = self.cleaned_data.get("contact_phone", "").strip()
        if not value:
            return ""
        phone = normalize_uz_phone(value)
        if not phone.startswith("+998 ") or len(phone) != 17:
            raise forms.ValidationError(_("Enter a valid Uzbek number: +998 XX XXX XX XX"))
        return phone


class PayPageForm(forms.ModelForm):
    """What the customer sees on /pay/<uid>/ and the limits we accept."""

    class Meta:
        model = SellerProfile
        fields = ("business_name", "welcome_text", "thank_you_text",
                  "min_amount", "max_amount")
        widgets = {
            "business_name": forms.TextInput(attrs={"class": "input"}),
            "welcome_text": forms.TextInput(attrs={"class": "input"}),
            "thank_you_text": forms.TextInput(attrs={"class": "input"}),
            "min_amount": forms.NumberInput(attrs={"class": "input", "min": 0, "step": 1000}),
            "max_amount": forms.NumberInput(attrs={"class": "input", "min": 0, "step": 1000}),
        }
        labels = {
            "business_name": _("Name shown to customers"),
            "welcome_text": _("Welcome text"),
            "thank_you_text": _("Thank-you text"),
            "min_amount": _("Smallest amount"),
            "max_amount": _("Largest amount"),
        }
        help_texts = {
            "welcome_text": _("Optional. Shown under your business name."),
            "thank_you_text": _("Optional. Shown after a successful payment."),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Amounts are Decimal so the arithmetic stays exact, but UZS has no
        # subunit in practice — showing a seller "1000.00" invites them to
        # wonder whether tiyin matter here. They do not.
        for name in ("min_amount", "max_amount"):
            value = self.initial.get(name)
            if value is not None:
                self.initial[name] = int(value)

    def clean(self):
        cleaned = super().clean()
        low, high = cleaned.get("min_amount"), cleaned.get("max_amount")
        # Not merely "not negative": a minimum of zero would let a customer
        # send a zero-soum payment, which no provider treats as real and which
        # would sit in the seller's ledger looking like one.
        if low is not None and low < 1:
            self.add_error("min_amount", _("The smallest amount must be at least 1 soum."))
        if low is not None and high is not None and high <= low:
            self.add_error(
                "max_amount", _("The largest amount must be above the smallest.")
            )
        return cleaned


class StyledPasswordChangeForm(PasswordChangeForm):
    """Django's own password change, wearing the TapCon input class.

    The labels are re-declared rather than inherited: Django ships no Uzbek
    translation for django.contrib.auth, so the inherited ones would render
    in English. Ours go through the project catalogue.
    """

    LABELS = {
        "old_password": _("Current password"),
        "new_password1": _("New password"),
        "new_password2": _("Repeat new password"),
    }

    # Django's own mismatch message is untranslated in uz and ru alike.
    error_messages = {
        **PasswordChangeForm.error_messages,
        "password_mismatch": _("The passwords do not match."),
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            field.label = self.LABELS.get(name, field.label)
            field.help_text = ""
            field.widget.attrs["class"] = "input"
            field.widget.attrs["autocomplete"] = (
                "current-password" if name == "old_password" else "new-password"
            )

from django import forms
from django.utils.translation import gettext_lazy as _

from accounts.models import normalize_uz_phone

from .models import CardOrder, ContactMessage

PHONE_ATTRS = {
    "class": "input",
    "placeholder": "+998 90 123 45 67",
    "inputmode": "tel",
    "autocomplete": "tel",
}


class UzPhoneMixin:
    def clean_phone(self):
        phone = normalize_uz_phone(self.cleaned_data["phone"])
        if not phone or not phone.startswith("+998 ") or len(phone) != 17:
            raise forms.ValidationError(_("Enter a valid Uzbek number: +998 XX XXX XX XX"))
        return phone


class CardOrderForm(UzPhoneMixin, forms.ModelForm):
    class Meta:
        model = CardOrder
        fields = ("full_name", "phone", "address", "quantity", "comment")
        labels = {
            "full_name": _("Your name"),
            "phone": _("Phone number"),
            "address": _("Delivery address"),
            "quantity": _("How many cards"),
            "comment": _("Comment"),
        }
        widgets = {
            "full_name": forms.TextInput(attrs={"class": "input", "autocomplete": "name"}),
            "phone": forms.TextInput(attrs=PHONE_ATTRS),
            "address": forms.TextInput(
                attrs={"class": "input", "placeholder": _("City, street, house")}
            ),
            "quantity": forms.NumberInput(attrs={"class": "input", "min": 1, "max": 500}),
            "comment": forms.Textarea(
                attrs={"class": "input", "rows": 3, "style": "height:auto;padding:12px 16px"}
            ),
        }
        help_texts = {
            "comment": _("Anything we should know — business type, delivery time."),
        }


class ContactForm(UzPhoneMixin, forms.ModelForm):
    class Meta:
        model = ContactMessage
        fields = ("full_name", "phone", "message")
        labels = {
            "full_name": _("Your name"),
            "phone": _("Phone number"),
            "message": _("Message"),
        }
        widgets = {
            "full_name": forms.TextInput(attrs={"class": "input", "autocomplete": "name"}),
            "phone": forms.TextInput(attrs=PHONE_ATTRS),
            "message": forms.Textarea(
                attrs={"class": "input", "rows": 4, "style": "height:auto;padding:12px 16px"}
            ),
        }

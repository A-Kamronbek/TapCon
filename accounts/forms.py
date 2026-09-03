from django import forms
from django.contrib.auth import authenticate, get_user_model
from django.contrib.auth.password_validation import validate_password
from django.utils.translation import gettext_lazy as _

from .models import normalize_uz_phone

User = get_user_model()

PHONE_WIDGET = forms.TextInput(
    attrs={"class": "input", "placeholder": "+998 90 123 45 67", "inputmode": "tel",
           "autocomplete": "tel"}
)


class PhoneFormMixin:
    """Normalise whatever the user typed before anything else looks at it."""

    def clean_phone(self):
        phone = normalize_uz_phone(self.cleaned_data["phone"])
        if not phone or not phone.startswith("+998 ") or len(phone) != 17:
            raise forms.ValidationError(_("Enter a valid Uzbek number: +998 XX XXX XX XX"))
        return phone


class RegisterForm(PhoneFormMixin, forms.Form):
    full_name = forms.CharField(
        label=_("Your name"),
        max_length=150,
        widget=forms.TextInput(attrs={"class": "input", "autocomplete": "name"}),
    )
    business_name = forms.CharField(
        label=_("Business name"),
        max_length=120,
        widget=forms.TextInput(attrs={"class": "input", "autocomplete": "organization"}),
    )
    phone = forms.CharField(label=_("Phone number"), max_length=20, widget=PHONE_WIDGET)
    password = forms.CharField(
        label=_("Password"),
        widget=forms.PasswordInput(attrs={"class": "input", "autocomplete": "new-password"}),
    )
    password_confirm = forms.CharField(
        label=_("Repeat password"),
        widget=forms.PasswordInput(attrs={"class": "input", "autocomplete": "new-password"}),
    )

    def clean(self):
        cleaned = super().clean()
        password = cleaned.get("password")
        confirm = cleaned.get("password_confirm")
        if password and confirm and password != confirm:
            self.add_error("password_confirm", _("The passwords do not match."))
        if password:
            try:
                validate_password(password)
            except forms.ValidationError as exc:
                self.add_error("password", exc)

        phone = cleaned.get("phone")
        if phone and User.objects.filter(phone=phone, phone_verified=True).exists():
            self.add_error("phone", _("This number is already registered."))
        return cleaned


class PhoneOnlyForm(PhoneFormMixin, forms.Form):
    phone = forms.CharField(label=_("Phone number"), max_length=20, widget=PHONE_WIDGET)


class OTPForm(forms.Form):
    code = forms.CharField(
        label=_("Confirmation code"),
        max_length=8,
        widget=forms.TextInput(
            attrs={
                "class": "input",
                "inputmode": "numeric",
                "autocomplete": "one-time-code",
                "placeholder": "000000",
            }
        ),
    )


class LoginForm(PhoneFormMixin, forms.Form):
    phone = forms.CharField(label=_("Phone number"), max_length=20, widget=PHONE_WIDGET)
    password = forms.CharField(
        label=_("Password"),
        widget=forms.PasswordInput(attrs={"class": "input", "autocomplete": "current-password"}),
    )

    def __init__(self, *args, request=None, **kwargs):
        self.request = request
        self.user = None
        super().__init__(*args, **kwargs)

    def clean(self):
        cleaned = super().clean()
        phone = cleaned.get("phone")
        password = cleaned.get("password")
        if phone and password:
            user = authenticate(self.request, username=phone, password=password)
            if user is None:
                raise forms.ValidationError(_("Wrong phone number or password."))
            if not user.is_active:
                raise forms.ValidationError(_("This account is disabled."))
            self.user = user
        return cleaned


class SetPasswordForm(forms.Form):
    password = forms.CharField(
        label=_("New password"),
        widget=forms.PasswordInput(attrs={"class": "input", "autocomplete": "new-password"}),
    )
    password_confirm = forms.CharField(
        label=_("Repeat password"),
        widget=forms.PasswordInput(attrs={"class": "input", "autocomplete": "new-password"}),
    )

    def clean(self):
        cleaned = super().clean()
        password = cleaned.get("password")
        confirm = cleaned.get("password_confirm")
        if password and confirm and password != confirm:
            self.add_error("password_confirm", _("The passwords do not match."))
        if password:
            try:
                validate_password(password)
            except forms.ValidationError as exc:
                self.add_error("password", exc)
        return cleaned

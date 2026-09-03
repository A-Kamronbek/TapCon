"""Credential forms built from the provider registry.

Adding a provider means adding a dict entry in providers.py — no new form
class, no new template. Three rules the whole design hangs on:

  * a stored SECRET is never rendered back into the HTML, not even hidden —
    the seller sees a mask and retypes it if they want to change it;
  * a stored identifier (Merchant ID, Service ID) IS shown, because it is
    not a secret and the seller has to be able to check it against the
    provider's cabinet without wiping it;
  * leaving a field blank keeps what is already stored, so a seller can
    change one key without retyping the rest.
"""
from decimal import Decimal

from django import forms
from django.utils.translation import gettext_lazy as _

from .providers import PROVIDERS


class AmountForm(forms.Form):
    """What the customer types on the pay page.

    The limits come from the seller, so the form is built per seller rather
    than declared once. Validating here as well as in `start_payment()` is
    deliberate: this produces a message next to the box, the service layer
    refuses the payment even if a request never came through this form.
    """

    # Whole soum only. UZS has no subunit in circulation, and a fractional
    # amount would reach the provider as a fractional tiyin — which they
    # variously reject or round, so the customer could be charged something
    # other than what the page showed them.
    amount = forms.DecimalField(
        label=_("Amount"),
        max_digits=12,
        decimal_places=0,
        widget=forms.NumberInput(
            attrs={
                "class": "input amount-input",
                "inputmode": "numeric",
                "step": "1000",
                "autocomplete": "off",
                "autofocus": "autofocus",
                "placeholder": "0",
            }
        ),
    )

    def __init__(self, *args, seller=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.seller = seller
        if seller is not None:
            field = self.fields["amount"]
            field.min_value = Decimal(seller.min_amount)
            field.max_value = Decimal(seller.max_amount)
            field.widget.attrs["min"] = int(seller.min_amount)
            field.widget.attrs["max"] = int(seller.max_amount)

    def clean_amount(self):
        amount = self.cleaned_data["amount"]
        if self.seller is None:
            return amount

        if amount < self.seller.min_amount:
            raise forms.ValidationError(
                _("The smallest amount here is %(amount)s soum.")
                % {"amount": f"{int(self.seller.min_amount):,}".replace(",", " ")}
            )
        if amount > self.seller.max_amount:
            raise forms.ValidationError(
                _("The largest amount here is %(amount)s soum.")
                % {"amount": f"{int(self.seller.max_amount):,}".replace(",", " ")}
            )
        return amount


def _numeric_credential(value):
    """A credential the provider's API types as a number.

    Blank passes: every box here is optional, and `clean()` decides separately
    whether a missing one is allowed.
    """
    value = (value or "").strip()
    if value and not value.isdigit():
        raise forms.ValidationError(_("Enter digits only."))


class ProviderCredentialsForm(forms.Form):
    """Fields come from the registry entry for one provider."""

    def __init__(self, *args, provider: str, integration=None, **kwargs):
        # Six of these forms render on one page and their field names overlap
        # (Click and Uzum both have service_id; Click and Paynet both have
        # merchant_id). With Django's default auto_id every one of them would
        # be id_service_id, so a <label for> would focus another provider's
        # box. Namespace the ids per provider.
        kwargs.setdefault("auto_id", f"id_{provider}_%s")
        super().__init__(*args, **kwargs)
        self.provider = provider
        self.integration = integration
        config = PROVIDERS[provider]

        for spec in config["fields"]:
            name = spec["name"]
            is_secret = spec.get("secret", False)
            stored = integration.get(name) if integration else ""

            attrs = {
                "class": "input",
                "autocomplete": "off",
                "spellcheck": "false",
            }
            if stored and is_secret:
                # Masked placeholder only: nothing usable ends up in the DOM.
                attrs["placeholder"] = integration.masked_value(name)
            if spec.get("numeric"):
                # Widget.__init__ copies this dict, so it has to be complete
                # before the field is built.
                attrs["inputmode"] = "numeric"

            self.fields[name] = forms.CharField(
                label=spec["label"],
                required=False,
                widget=forms.TextInput(attrs=attrs),
                help_text=(
                    _("Leave blank to keep the saved value.")
                    if (stored and is_secret)
                    else ""
                ),
            )
            if stored and not is_secret and not self.is_bound:
                self.initial.setdefault(name, stored)

            if spec.get("numeric"):
                # Octo's shop id goes into its JSON body as a number. Catch a
                # pasted "ID-42125" here, where the seller can see the
                # message — not at a customer's payment.
                self.fields[name].validators.append(_numeric_credential)

            self.fields[name].is_secret = is_secret
            self.fields[name].is_numeric = bool(spec.get("numeric"))
            self.fields[name].has_stored_value = bool(stored)

    def merged_credentials(self) -> dict:
        """Submitted values, falling back to what is already stored.

        Blank always means "leave it alone". Emptying a box is not a way to
        delete a credential — `clean()` rejects an incomplete provider first,
        so a seller who clears a field is told to fill it in rather than
        silently getting the old value back. Removing a provider entirely is
        switching it off, which is its own button.
        """
        stored = dict(self.integration.credentials) if self.integration else {}
        for name, value in self.cleaned_data.items():
            value = (value or "").strip()
            if value:
                stored[name] = value
        return stored

    def clean(self):
        """Refuse a half-filled provider.

        Every box is `required=False` so that a seller can change one key
        without retyping the secret. That is about the *box*, not the
        credential: what has to be complete is the merged result. Without
        this, Click could be saved with only a Service ID — stored, shown as
        configured, and quietly unusable.
        """
        cleaned = super().clean()
        for name, value in list(cleaned.items()):
            if isinstance(value, str):
                cleaned[name] = value.strip()

        stored = self.integration.credentials if self.integration else {}
        submitted_anything = any(cleaned.get(n) for n in self.fields)
        if not (submitted_anything or stored):
            # Nothing typed and nothing saved: an empty Save is a no-op, not
            # an error to shout about.
            return cleaned

        for name in self.fields:
            if cleaned.get(name):
                continue
            # A secret is never rendered back, so an empty box can only mean
            # "I did not retype it". An identifier IS rendered back, so an
            # empty one is the seller having cleared it — say so instead of
            # quietly restoring the old value under them.
            if self.fields[name].is_secret and stored.get(name):
                continue
            self.add_error(name, _("Fill this in."))
        return cleaned

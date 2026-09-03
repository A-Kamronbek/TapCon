"""Which of Django's own messages reach a seller in English?

Django ships an incomplete uz catalogue. Anything listed here renders in
English on a page a seller sees, so it needs overriding in our own code.

The messages are pulled from the real validators and forms rather than
typed out here by hand: hand-typed samples miss when Django uses ngettext
(the singular is not a lookup key) or a typographic apostrophe, and then
the check reports a translation gap that does not exist.
"""
import os
import pathlib
import sys

import django

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")
django.setup()

# Windows consoles default to cp1252, which cannot print Cyrillic. Without
# this the ru pass dies half way and the check silently covers only uz.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from django.apps import apps  # noqa: E402
from django.contrib.auth import forms as auth_forms  # noqa: E402
from django.contrib.auth import password_validation  # noqa: E402
from django.core.exceptions import ValidationError  # noqa: E402
from django.utils import translation  # noqa: E402

User = apps.get_model("accounts", "User")

# Passwords chosen to trip each configured validator in turn.
BAD_PASSWORDS = ("a", "password", "12345678", "901234567")


def messages():
    """Every message the password validators and auth forms can show.

    Returned in a fixed order so the same call under two languages yields
    two lists that line up position by position.
    """
    out = []
    user = User(phone="+998901234567")
    for validator in password_validation.get_default_password_validators():
        for candidate in BAD_PASSWORDS:
            try:
                validator.validate(candidate, user)
            except ValidationError as exc:
                out.extend(str(m) for m in exc.messages)
            else:
                out.append("")  # keep positions aligned across languages
        out.append(str(validator.get_help_text()))

    # TapCon's own subclass, not Django's, because that is what the settings
    # page renders — Django's raw labels are never shown to anyone.
    from merchants.forms import StyledPasswordChangeForm

    for form_class in (
        auth_forms.AuthenticationForm,
        StyledPasswordChangeForm,
        auth_forms.SetPasswordForm,
    ):
        try:
            form = form_class(User(phone="+998901234567"))
        except TypeError:
            form = form_class()
        for field in form.fields.values():
            out.append(str(field.label or ""))
            out.extend(str(m) for m in field.error_messages.values())
        out.extend(str(m) for m in form.error_messages.values())
    return out


def main():
    with translation.override("en"):
        english = messages()

    failures = 0
    for lang in ("uz", "ru"):
        with translation.override(lang):
            localised = messages()
        if len(localised) != len(english):
            print(f"=== {lang}: message list changed shape, cannot compare ===")
            failures += 1
            continue
        missing = sorted({
            src for src, dst in zip(english, localised)
            if src.strip() and src == dst
        })
        print(f"=== {lang} ({len(english)} messages) ===")
        for text in missing:
            print(f"  [ENGLISH] {text}")
        if not missing:
            print("  all translated")
        failures += len(missing)

    print()
    if failures:
        print(f"{failures} Django message(s) reach a seller in English")
        return 1
    print("OK - no Django message reaches a seller in English")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

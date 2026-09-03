"""Password validators with messages that actually exist in Uzbek.

Django ships an incomplete `uz` catalogue: every password-validation message
is untranslated there, so a seller entering a weak password would read an
English error on the primary language. These subclasses keep Django's logic
and replace only the wording, routing it through the project catalogue.
"""
from django.contrib.auth import password_validation as dj
from django.core.exceptions import ValidationError
from django.utils.translation import gettext as _


class MinimumLengthValidator(dj.MinimumLengthValidator):
    def validate(self, password, user=None):
        if len(password) < self.min_length:
            raise ValidationError(
                _("The password must be at least %(min_length)d characters long."),
                code="password_too_short",
                params={"min_length": self.min_length},
            )

    def get_help_text(self):
        return _("At least %(min_length)d characters.") % {"min_length": self.min_length}


class CommonPasswordValidator(dj.CommonPasswordValidator):
    def validate(self, password, user=None):
        try:
            super().validate(password, user)
        except ValidationError:
            raise ValidationError(
                _("This password is too common. Choose a less obvious one."),
                code="password_too_common",
            )

    def get_help_text(self):
        return _("Do not use a common password.")


class NumericPasswordValidator(dj.NumericPasswordValidator):
    def validate(self, password, user=None):
        try:
            super().validate(password, user)
        except ValidationError:
            raise ValidationError(
                _("The password cannot be only numbers."),
                code="password_entirely_numeric",
            )

    def get_help_text(self):
        return _("The password cannot be only numbers.")


class UserAttributeSimilarityValidator(dj.UserAttributeSimilarityValidator):
    def validate(self, password, user=None):
        try:
            super().validate(password, user)
        except ValidationError:
            raise ValidationError(
                _("The password is too similar to your other details."),
                code="password_too_similar",
            )

    def get_help_text(self):
        return _("The password cannot be too similar to your other details.")

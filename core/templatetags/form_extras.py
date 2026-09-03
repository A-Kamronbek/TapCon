"""Small form-rendering helpers.

Django gives a bound field no way to say "you failed validation" in the
markup itself, so an invalid input would look identical to a valid one.
This adds the class at render time without touching the form classes.
"""
from django import template

register = template.Library()


@register.filter
def add_error_class(field, css_class="is-invalid"):
    """Render a bound field with an extra CSS class merged into its widget."""
    existing = field.field.widget.attrs.get("class", "")
    merged = f"{existing} {css_class}".strip()
    return field.as_widget(attrs={"class": merged})

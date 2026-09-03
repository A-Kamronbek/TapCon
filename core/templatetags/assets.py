"""Static files that cannot be served stale.

A cached stylesheet once made a finished fix look broken: the page rendered
with an old `layouts.css` and a provider logo came out at its full intrinsic
size. Nothing was wrong with the code, and that is exactly what made it cost
time to find.

`{% static_v 'css/x.css' %}` appends the file's modification time, so the URL
changes whenever the file does and a browser cannot hold on to the old one.
In production WhiteNoise hashes filenames and this becomes a no-op, but the
tag stays correct either way.
"""
import os

from django import template
from django.contrib.staticfiles import finders
from django.templatetags.static import static

register = template.Library()

# Cheap in dev (a stat per file per render), and skipped entirely once
# ManifestStaticFilesStorage is doing the versioning in production.
_CACHE: dict[str, str] = {}


@register.simple_tag
def static_v(path: str) -> str:
    url = static(path)

    absolute = finders.find(path)
    if not absolute:
        # Collected or missing: nothing to stamp, and a missing file is the
        # markup checker's job to report, not this tag's.
        return url

    try:
        stamp = str(int(os.path.getmtime(absolute)))
    except OSError:
        return url

    separator = "&" if "?" in url else "?"
    return f"{url}{separator}v={stamp}"

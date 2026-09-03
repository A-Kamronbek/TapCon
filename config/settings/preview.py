"""Development settings with DEBUG off, for looking at what production shows.

Two things are invisible while DEBUG is on, and both are things a customer
sees: the branded error pages (Django serves its own debug pages instead) and
the bundled stylesheet (base.html links the eight sources).

    python manage.py runserver --insecure --settings=config.settings.preview

`--insecure` is what makes runserver keep serving static files with DEBUG off.
This is a local looking-glass, never a deployment target — prod.py is the real
production settings, with WhiteNoise, PostgreSQL and the security headers.
"""
from .dev import *  # noqa: F401,F403

DEBUG = False

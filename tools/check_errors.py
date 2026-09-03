"""Show what a seller actually reads when a form rejects their input."""
import os
import pathlib
import re
import sys

import django

# The Windows console defaults to cp1252, which cannot print Cyrillic.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")
django.setup()

from django.test import Client  # noqa: E402
from django.test.utils import setup_test_environment  # noqa: E402

setup_test_environment()

CASES = [
    ("weak password", {"password": "12345", "password_confirm": "12345"}),
    ("common password", {"password": "password", "password_confirm": "password"}),
    ("mismatch", {"password": "Str0ng-pass!x", "password_confirm": "different"}),
    ("bad phone", {"phone": "12345", "password": "Str0ng-pass!x",
                   "password_confirm": "Str0ng-pass!x"}),
    ("empty", {"full_name": "", "business_name": "", "phone": "",
               "password": "", "password_confirm": ""}),
]

for lang in ("uz", "ru"):
    print(f"\n===== {lang} =====")
    for name, overrides in CASES:
        data = {
            "full_name": "Ali",
            "business_name": "Anor",
            "phone": "+998 90 777 66 55",
            **overrides,
        }
        response = Client().post(f"/{lang}/auth/register/", data)
        html = response.content.decode("utf-8")
        errors = re.findall(r'class="(?:field-error|toast toast-error)">\s*(.*?)\s*</div>',
                            html, re.S)
        cleaned = [re.sub(r"<[^>]+>", " ", e).strip() for e in errors]
        cleaned = [re.sub(r"\s+", " ", c) for c in cleaned if c]
        print(f"  {name}: {cleaned}")

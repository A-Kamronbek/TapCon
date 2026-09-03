"""What the pay page costs a customer on a phone, counted rather than guessed.

    python tools/check_weight.py

Renders the page the way production will — one bundled stylesheet, the
self-hosted font — and adds up every byte a first-time visitor downloads,
gzipped, the way Nginx will serve them. Fails if it goes over budget.

The budget is not aspirational: it is a little above where the page sits
today, so that adding something expensive to the product's most important
page is a decision someone has to make on purpose.
"""
import gzip
import os
import pathlib
import re
import sys

import django

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")
django.setup()

from django.test import Client, override_settings  # noqa: E402
from django.test.utils import setup_test_environment  # noqa: E402

from merchants.models import SellerProfile  # noqa: E402

setup_test_environment()
ROOT = pathlib.Path(__file__).resolve().parent.parent

# On the wire, gzipped, first visit, every provider the seller has enabled.
BUDGET_KB = 110
REQUEST_BUDGET = 10

# Already-compressed formats: gzip does nothing for them, and Nginx will not
# waste CPU trying.
INCOMPRESSIBLE = {".woff2", ".png", ".jpg", ".webp", ".ico"}


def wire_size(data: bytes, suffix: str) -> int:
    if suffix in INCOMPRESSIBLE:
        return len(data)
    return len(gzip.compress(data, 6))


def main() -> int:
    profile = (
        SellerProfile.objects.filter(status="approved").first()
        or SellerProfile.objects.first()
    )
    if profile is None:
        print("no seller in the database to render a pay page for")
        return 1

    # DEBUG off is what makes base.html link the bundle instead of the eight
    # sources, which is the thing being measured.
    with override_settings(DEBUG=False):
        html = Client().get(f"/pay/{profile.uid}/").content

    rows = [("the page itself (HTML)", len(html), wire_size(html, ".html"))]

    # Everything the page asks for, read off the markup rather than assumed —
    # including the preloaded font, which appears there as a link.
    seen = set()
    for match in sorted(set(re.findall(rb'(?:href|src)="/static/([^"?]+)', html))):
        name = match.decode()
        if name in seen:
            continue
        seen.add(name)
        path = ROOT / "static" / name
        if not path.exists():
            rows.append((f"{name}  (MISSING)", 0, 0))
            continue
        data = path.read_bytes()
        rows.append((name, len(data), wire_size(data, path.suffix)))

    print(f"{'asset':<44}{'raw':>10}{'on the wire':>14}")
    print("-" * 68)
    for name, raw, wire in rows:
        print(f"{name:<44}{raw / 1024:>9.1f}K{wire / 1024:>13.1f}K")
    print("-" * 68)

    total = sum(w for _, _, w in rows) / 1024
    print(f"{'TOTAL, first visit':<44}{sum(r for _, r, _ in rows) / 1024:>9.1f}K"
          f"{total:>13.1f}K")
    print(f"\n{len(rows)} requests, {total:.1f} KB on the wire "
          f"(budget: {REQUEST_BUDGET} requests, {BUDGET_KB} KB)")
    print("A repeat visitor downloads the HTML only — everything else is cached.")

    problems = []
    if total > BUDGET_KB:
        problems.append(f"{total:.1f} KB is over the {BUDGET_KB} KB budget")
    if len(rows) > REQUEST_BUDGET:
        problems.append(
            f"{len(rows)} requests is over the {REQUEST_BUDGET} allowed"
        )
    missing = [n for n, _, w in rows if "MISSING" in n]
    if missing:
        problems.append(f"missing static file(s): {', '.join(missing)}")

    if problems:
        print("\n" + "\n".join(f"FAIL: {p}" for p in problems))
        return 1

    print("\nOK - the pay page is within budget")
    return 0


if __name__ == "__main__":
    sys.exit(main())

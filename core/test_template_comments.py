"""A {# #} comment that spans two lines is not a comment.

Django's `{# ... #}` is single-line only. Write one across several lines and
Django comments out the first line and renders the rest as page text — so a
note to the next developer appears on the page, in every language, and any
markup inside it becomes real markup. That is exactly how a stray <main>
ended up in the sign-in page.

It is silent: the template compiles, the tests pass, the page returns 200.
Only a reader notices. So it is checked here, over every template at once,
rather than trusted to review.
"""
import pathlib

from django.conf import settings
from django.test import SimpleTestCase

TEMPLATE_DIRS = [
    pathlib.Path(d) for engine in settings.TEMPLATES
    for d in engine.get("DIRS", [])
]


class SingleLineCommentTests(SimpleTestCase):
    def templates(self):
        for root in TEMPLATE_DIRS:
            yield from sorted(root.rglob("*.html"))

    def test_the_sweep_actually_found_templates(self):
        self.assertGreater(len(list(self.templates())), 20)

    def test_no_hash_comment_is_left_open(self):
        offenders = []
        for path in self.templates():
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                # An opening {# with no closing #} on the same line means the
                # rest of the comment is being rendered as page content.
                if line.count("{#") > line.count("#}"):
                    offenders.append(f"{path.name}:{number}: {line.strip()[:70]}")
        self.assertEqual(
            offenders,
            [],
            "\n".join(
                ["{# #} does not span lines - use {% comment %}:"] + offenders
            ),
        )

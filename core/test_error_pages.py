"""The 500 page has to render when everything else is broken.

Django renders 500.html with an empty context and no context processors, so
anything the template reaches for — a {% url %}, a static file, the language
switcher, a request variable — is either missing or, worse, raises a second
exception while handling the first. The seller then gets a bare stack trace
from the WSGI server instead of a page.

The database being down is the case that matters: it is the most likely
cause of a 500 in the first place, and any query the error page triggers
fails too.
"""
from django.template import loader
from django.test import RequestFactory, TestCase
from django.views.defaults import page_not_found, server_error


class ServerErrorPageTests(TestCase):
    databases = []  # no database, on purpose: prove the page needs none

    def test_renders_with_no_context_at_all(self):
        html = loader.get_template("500.html").render()
        self.assertIn("<html", html.lower())
        self.assertGreater(len(html), 200)

    def test_default_handler_renders_it(self):
        request = RequestFactory().get("/uz/merchant/")
        response = server_error(request)
        self.assertEqual(response.status_code, 500)
        self.assertIn(b"<html", response.content.lower())

    def test_carries_no_link_to_a_stylesheet_or_script(self):
        """Static files may be unreachable in the failure that caused the 500."""
        html = loader.get_template("500.html").render()
        self.assertNotIn("<link", html.lower())
        self.assertNotIn("<script", html.lower())

    def test_gives_the_reader_a_way_back(self):
        html = loader.get_template("500.html").render()
        self.assertIn('href="/"', html)


class NotFoundPageTests(TestCase):
    def test_handler_renders_the_branded_page(self):
        request = RequestFactory().get("/uz/nope/")
        response = page_not_found(request, Exception())
        self.assertEqual(response.status_code, 404)
        self.assertIn(b"TapCon", response.content)

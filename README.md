# TapCon

[![CI](https://github.com/A-Kamronbek/TapCon/actions/workflows/ci.yml/badge.svg)](https://github.com/A-Kamronbek/TapCon/actions/workflows/ci.yml)

NFC-card payment service for Uzbekistan. A customer taps a seller's NFC card,
their phone opens `tapcon.uz/pay/<uid>`, they enter an amount and pay with
Click, Payme, Uzum, Paynet or a bank card. Sellers manage their own provider
credentials in a merchant portal — TapCon is multi-tenant and never holds
global provider credentials.

**Status:** the website is complete. The service has not been launched to the
public yet and has no users.

## Stack

- Django 6.1 (Python 3.14), server-rendered templates — no JS framework
- SQLite in development, PostgreSQL in production
- [`tolov`](https://pypi.org/project/tolov/) 2.2 for Click / Payme / Uzum / Paynet / Octo / Multicard
- Uzbek (default), Russian, English via Django i18n
- Auth by phone number + SMS OTP (no email)

## Layout

```
config/          settings (base/dev/prod/pgtest), root urls, wsgi/asgi
core/            marketing pages, card orders, contact, rate limiting
accounts/        phone-based User, OTP auth
merchants/       seller profile, provider credentials, dashboard
payments/        pay page, transactions, tolov gateways, webhooks
templates/       base.html + marketing/merchant/pay layouts
static/css/      tokens.css -> components.css -> layouts.css
locale/          uz, ru, en translations
tools/           verification scripts that drive a running server
deploy/          backup, restore drill, server access
```

Business logic lives in each app's `services.py` so the planned v2 DRF API
can reuse it without a rewrite. Every figure a seller is shown is computed in
`payments/reporting.py` — no view and no template sums an amount.

## Getting started

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows
pip install -r requirements.txt
copy .env.example .env            # then fill SECRET_KEY and FIELD_ENCRYPTION_KEY
python manage.py migrate
python manage.py createsuperuser  # asks for a phone number, not an email
python manage.py runserver
```

Open http://127.0.0.1:8000/ — it redirects to `/uz/`. `/ru/` and `/en/` work too.
`/uz/styleguide/` renders every design-system component.

### Signing up as a seller

Registration is by phone + SMS code. In development `SMS_BACKEND = "console"`,
so **the code is printed in the runserver output** — no SMS is sent and no
Eskiz credentials are needed. Production uses the Eskiz backend
(`ESKIZ_EMAIL` / `ESKIZ_PASSWORD` in `.env`); note Eskiz only delivers message
texts that have passed their moderation, so the OTP templates in
`docs/eskiz-templates.md` must be submitted to them before go-live.

A new seller lands in `pending`. Approve them in the admin
(Sellers -> select -> "Approve selected sellers") before their `/pay/<uid>/`
page will take money.

### URLs and languages

User-facing pages are language-prefixed (`/uz/...`, `/ru/...`, `/en/...`).
Two things deliberately are **not**: `/pay/<uid>/`, because that address is
written once onto a physical NFC card, and `/webhooks/...`, because providers
call a fixed URL. Both pick the language from the visitor's cookie or
`Accept-Language`; the switcher posts to `set_language`.

`manage.py` defaults to `config.settings.dev`. Production runs with
`DJANGO_SETTINGS_MODULE=config.settings.prod` and requires `DATABASE_URL`.

## Multi-tenancy, and why the webhooks look the way they do

`tolov` was written for a single-tenant shop: its webhook handlers read
`settings.TOLOV[...]` **once, in `__init__`**, and copy the credentials onto
the instance. TapCon has a different merchant account per seller, so a global
key would be wrong for everyone but the first.

Django builds a fresh view instance per request and calls `setup()` before
`dispatch()`, so `payments/webhooks.py` resolves the seller from the URL in
`setup()` and overwrites those attributes with theirs. No fork of `tolov` is
needed. `settings.TOLOV` therefore holds no credentials — only deliberately
unusable placeholders for the two handlers that refuse to be constructed
without them, so a failed injection fails *closed*.

## Testing

```bash
python manage.py test                                   # 450 tests, SQLite
python manage.py test --settings=config.settings.pgtest # the same, on PostgreSQL
```

**Run the PostgreSQL pass before deploying.** SQLite ignores
`SELECT ... FOR UPDATE` entirely, so every row lock that stops a webhook
processing the same payment twice is untested under it — the tests pass
whether the lock is there or not. `payments/test_concurrency.py` fires eight
workers at one payment through a barrier and **skips** on SQLite rather than
passing dishonestly. Point `PGTEST_DATABASE_URL` at any throwaway database.

That suite found a defect no amount of single-threaded testing could: two
simultaneous `CreateTransaction` callbacks both opened a charge against one
payment, so a customer with two tabs open could be asked to pay twice while
only one payment could ever be recorded.

### The verification tools

A green unit suite is not proof for webhooks. Every provider-side defect found
in this project was invisible to the test suite and obvious to a script
driving real signed HTTP at a running server. These need
`python manage.py runserver 127.0.0.1:8009 --insecure --settings=config.settings.preview`
and `CRAWL_BASE` / `CRAWL_PHONE` / `CRAWL_PASSWORD` set.

| Tool | What it does |
|---|---|
| `tools/live_callbacks.py` | Real signed callbacks for Octo and Multicard |
| `tools/probe_providers.py` | Payme, Click, Uzum and Paynet, each in its own shape |
| `tools/verify_money.py` | Recomputes every reported figure from raw rows, then checks the rendered page |
| `tools/live_crawl.py` | Every route × 3 languages, GET and HEAD, signed in and out |
| `tools/audit_pages.py` | Non-200s, English leaking into Uzbek, credentials in the HTML |
| `tools/check_markup.py` | Structural faults and undefined CSS variables |
| `tools/check_forms.py` | Junk into every form, in every language |
| `tools/check_errors.py` | Form errors exactly as a seller reads them |
| `tools/check_weight.py` | Pay-page budget: bytes on the wire and request count |
| `tools/check_django_uz.py` | What Django's own incomplete Uzbek catalogue leaves in English |
| `tools/check_pay_language.py` | Which language each phone gets on the unprefixed pay page |
| `tools/check_credentials.py` | That Telegram and Eskiz actually work — read-only, spends nothing |

## Translations

Uzbek is the default language and the catalogues are kept **complete** — a
test fails if any string is untranslated or left fuzzy.

```bash
python manage.py makemessages -l uz -l ru -l en --no-wrap --ignore=.venv
python tools/translate.py uz --report   # fills from tools/translations/uz_*.json
python tools/translate.py ru --report
python manage.py compilemessages
```

Add new strings to `tools/translations/<lang>_<phase>.json`; `--report` lists
whatever is still missing, and flags map entries whose msgid no longer exists.

Three traps this tooling exists to avoid:

- **Never bulk-strip `#, fuzzy`.** A fuzzy entry is gettext's *guess*, and
  promoting one silently ships a wrong translation — one `makemessages` run
  here turned "Previous" into the Uzbek for "Appearance".
  `tools/translate.py` blanks unmapped fuzzy entries instead.
- **De-fuzzing must clear the `#|` comments too.** They are legal only on a
  fuzzy entry, and one left behind makes `compilemessages` reject the whole
  catalogue with a bare "syntax error".
- **Don't parse `.po` with regexes.** xgettext wraps long msgids across lines
  and a regex quietly skips them. The tool uses `polib`.

Django's own Uzbek catalogue is incomplete — the password-validation messages
are missing entirely — so `accounts/validators.py` subclasses those validators
with our own wording, and `merchants/forms.py` re-labels the password-change
form. `tools/check_django_uz.py` reports what else Django leaves untranslated.

No "does this look like English" heuristic can work here, because Uzbek is
written in the Latin alphabet. `audit_pages.py` compares against the
catalogue instead.

## Deployment

Ubuntu with Nginx, Gunicorn and PostgreSQL. See `deploy/` for the backup
script, the restore drill and the server access procedure.

```
pull -> migrate -> createcachetable -> bundlecss -> collectstatic
     -> compilemessages -> restart
```

Order matters: `bundlecss` before `collectstatic`, or the previous stylesheet
bundle is what ships; and `*.mo` is gitignored, so `compilemessages` is not
optional.

Production refuses to start on a placeholder `SECRET_KEY`, a placeholder
`FIELD_ENCRYPTION_KEY`, or a `SITE_URL` still pointing at localhost — that
last one is the address payment providers are told to call back on, so left
at the development default every card payment charges the customer and
records nothing.

`manage.py createcachetable` is required: the rate limits are counted in a
database cache so that Gunicorn's workers share one number, and without the
table they silently fail open.

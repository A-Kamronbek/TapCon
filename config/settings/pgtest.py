"""Dev settings with PostgreSQL instead of SQLite, for running the test suite.

Why this exists: SQLite silently ignores ``SELECT ... FOR UPDATE``. Every row
lock that stops a webhook from processing the same payment twice is therefore
untested under SQLite — the tests pass whether the lock is there or not.
PostgreSQL is also strict about column types where SQLite coerces, which is how
the Click decline crash (a string written into an IntegerField) stayed hidden.

Run the suite against a real PostgreSQL before every deploy:

    set PGTEST_DATABASE_URL=postgres://postgres@127.0.0.1:5432/tapcon_verify
    python manage.py test --settings=config.settings.pgtest

Any throwaway database works; Django creates and drops ``test_<name>`` itself.
"""
from .dev import *  # noqa: F401,F403
from .dev import env

DATABASES = {
    "default": env.db(
        "PGTEST_DATABASE_URL",
        default="postgres://postgres@127.0.0.1:5432/tapcon_verify",
    )
}

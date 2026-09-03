# Restoring TapCon

A backup nobody has restored is a hypothesis. Run this drill once before
go-live and once every few months after, on a throwaway VM — not on the
production server.

## The thing most likely to go wrong

**`FIELD_ENCRYPTION_KEY` is not in the database.** Every seller's Payme,
Click, Uzum, Paynet and Octo credentials are encrypted with it, and it lives
only in `.env`.

Restore a dump with a different key and you get a site that looks completely
healthy — sellers can sign in, the ledger is intact, every past payment is
there — and **not one seller can take a payment** until they re-enter their
provider credentials by hand. There is no recovery from a lost key, because
that is what encryption at rest means. `deploy/backup.sh` therefore backs up
an encrypted copy of `.env` alongside the dump, and this drill is what proves
the two halves fit together.

Keep `BACKUP_PASSPHRASE` in a password manager, not on the server. A stolen
server should not hand over both halves at once.

**The wrong key does not raise an error.** `django-encrypted-model-fields`
hands back an *empty* value rather than failing, so a restore with the wrong
key looks identical to a fresh install where nobody has configured a provider
yet. That is precisely why `verify_restore` treats "every credential
decrypted to nothing" as a failure instead of an empty database — without
that, the drill below would pass on a restore that is worthless.

## The drill

```bash
# 1. A clean database on a throwaway machine.
createdb tapcon_restore_test

# 2. The dump.
pg_restore --no-owner --no-privileges \
           --dbname=postgres://user:pass@localhost/tapcon_restore_test \
           tapcon-2026-09-02-0215.dump

# 3. The secrets that make it readable.
openssl enc -d -aes-256-cbc -pbkdf2 \
        -in tapcon-env-2026-09-02-0215.enc -out .env.restored \
        -pass env:BACKUP_PASSPHRASE

# 4. Point a checkout at both and run the verification below.
cp .env.restored .env
sed -i 's|^DATABASE_URL=.*|DATABASE_URL=postgres://user:pass@localhost/tapcon_restore_test|' .env
python manage.py migrate --check --settings=config.settings.prod
python manage.py verify_restore --settings=config.settings.prod
```

`verify_restore` is the step that matters. Anyone can restore a dump; the
question is whether the credentials in it can still be decrypted, and that is
what it answers.

## What to check by hand afterwards

- Sign in as a seller. The dashboard totals should match what you remember.
- Open **Integrations**. Every provider that was connected should still show
  as connected, with its fields masked but present. A provider that has
  quietly emptied is the symptom of a wrong `FIELD_ENCRYPTION_KEY`.
- Open a pay page and start a sandbox payment. It should reach the provider.

## If the key really is lost

There is no way back. What to do:

1. Generate a new `FIELD_ENCRYPTION_KEY`.
2. Clear the credential blobs (`ProviderIntegration.credentials_blob = ""`)
   and set every integration to `is_enabled=False`, so no pay page offers a
   provider that cannot work.
3. Tell every seller, and walk them through re-entering their keys.

Transactions, sellers, accounts and card orders are all unaffected — they are
not encrypted. It is only the provider credentials that are gone.

## Cron

```cron
15 2 * * *  BACKUP_PASSPHRASE=... BACKUP_REMOTE=b2:tapcon-backups /opt/tapcon/deploy/backup.sh >> /var/log/tapcon-backup.log 2>&1
```

Check the log has a line from last night before you believe any of this.

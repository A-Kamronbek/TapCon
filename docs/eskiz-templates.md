# Eskiz SMS templates — for moderation

Submit both of these at **notify.eskiz.uz**, under SMS → Шаблоны (templates).

Eskiz approves **one exact string per template** and rejects any message that
does not match it character for character. These texts are therefore *not*
translated: the message stays identical whatever language the visitor is
browsing the site in.

They are copied here straight from `accounts/services.py` → `SMS_TEMPLATES`.
**If you change the wording in either place, change it in both, and submit the
new text for moderation again.** A test compares this file to the code.

---

## 1. Registration code

```
Tapcon.uz saytidan ro'yxatdan o'tish uchun kodingiz: 123456
```

Sent when someone registers a new business account and has to confirm their
phone number.

## 2. Password reset code

```
Tapcon.uz saytida parolni tiklash uchun kodingiz: 123456
```

Sent when someone asks to reset the password on an existing account.

---

## Notes for the submission form

- **`123456` is a placeholder.** Eskiz expects a sample; the real message
  carries a six-digit code in that position. If their form wants a variable
  marker instead, the code substitutes at that exact spot and nothing else in
  the string changes.
- **The apostrophe is a plain ASCII `'`** (U+0027), not a curly quote. Copy
  the lines above rather than retyping them — a typographic apostrophe is a
  different character and the messages would stop matching.
- **Spelling:** `ro'yxatdan`, not `ro'yhatdan`. This was checked; the first is
  correct Uzbek.
- **Sender name** is whatever Eskiz has approved for the account. Nothing in
  the code depends on it.

## After approval

Set these in the production `.env` and switch the backend over:

```
SMS_BACKEND=eskiz
ESKIZ_EMAIL=...
ESKIZ_PASSWORD=...
```

Until then `SMS_BACKEND` stays on the console backend, which prints the code
to the server log instead of sending it.

"""Apply a JSON translation map to a .po catalogue.

Run from the repo root:

    python manage.py makemessages -l uz -l ru -l en --no-wrap --ignore=.venv
    python tools/translate.py uz
    python tools/translate.py ru --report
    python manage.py compilemessages

Maps live in tools/translations/<lang>.json as {"msgid": "translation"}.

Two rules that exist because getting them wrong silently corrupts the site:

1. A fuzzy entry is gettext's *guess*, not a translation. We never promote one.
   Entries we have no map value for are blanked and de-fuzzed, so the page
   falls back to English instead of showing a wrong translation.
2. polib parses the real .po grammar, including msgids wrapped across lines.
   A regex over the file text misses those.
"""
import argparse
import json
import pathlib
import sys

import polib

ROOT = pathlib.Path(__file__).resolve().parent.parent
MAPS = pathlib.Path(__file__).resolve().parent / "translations"


def catalogue(lang):
    return ROOT / "locale" / lang / "LC_MESSAGES" / "django.po"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("lang")
    parser.add_argument("--report", action="store_true", help="list untranslated msgids")
    args = parser.parse_args()

    po_path = catalogue(args.lang)
    if not po_path.exists():
        print(f"no catalogue at {po_path} - run makemessages first")
        return 1

    # <lang>.json plus any <lang>_*.json, so a phase can add its own file.
    mapping = {}
    for path in sorted(MAPS.glob(f"{args.lang}.json")) + sorted(
        MAPS.glob(f"{args.lang}_*.json")
    ):
        mapping.update(json.loads(path.read_text(encoding="utf-8")))
    po = polib.pofile(str(po_path), encoding="utf-8")

    filled = defuzzed = 0
    for entry in po:
        if entry.obsolete or not entry.msgid:
            continue
        value = mapping.get(entry.msgid)

        if entry.msgid_plural:
            # Plural entries take a list, one string per plural form:
            # uz has 1 form, ru has 4.
            if isinstance(value, list) and value:
                forms = len(entry.msgstr_plural) or len(value)
                entry.msgstr_plural = {
                    i: value[min(i, len(value) - 1)] for i in range(forms)
                }
                filled += 1
            elif "fuzzy" in entry.flags:
                entry.msgstr_plural = {i: "" for i in entry.msgstr_plural}
                defuzzed += 1
        elif value:
            entry.msgstr = value
            filled += 1
        elif "fuzzy" in entry.flags:
            entry.msgstr = ""
            defuzzed += 1
        if "fuzzy" in entry.flags:
            entry.flags.remove("fuzzy")
            # The `#|` lines record what gettext guessed this entry used to
            # be. They are only legal on a fuzzy entry, so leaving one behind
            # after de-fuzzing makes msgfmt refuse the whole catalogue with a
            # bare "syntax error" and a line number — every one of them has
            # to go, not just previous_msgid.
            entry.previous_msgid = None
            entry.previous_msgid_plural = None
            entry.previous_msgctxt = None

    po.metadata["Content-Type"] = "text/plain; charset=UTF-8"
    po.save(str(po_path))

    def untranslated(e):
        if e.msgid_plural:
            return not any(e.msgstr_plural.values())
        return not e.msgstr

    missing = [e.msgid for e in po if not e.obsolete and e.msgid and untranslated(e)]
    print(f"{args.lang}: {filled} translated, {defuzzed} fuzzy cleared, {len(missing)} missing")

    if args.report and missing:
        print("--- untranslated ---")
        for msgid in missing:
            print(msgid)

    unused = sorted(set(mapping) - {e.msgid for e in po if not e.obsolete})
    if unused:
        print(f"--- {len(unused)} map entries no longer in the catalogue ---")
        for msgid in unused:
            print(msgid)

    return 0


if __name__ == "__main__":
    sys.exit(main())

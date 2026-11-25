#!/usr/bin/env python3
"""Run the clean-copy linter on the fixtures and on false-positive guards.

Usage: python3 tests/run_tests.py
Exit status 0 when every expectation holds, 1 otherwise.
"""
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
LINTER = HERE.parent / "scripts" / "clean_copy_check.py"

# (fixture, extra args, expected exit code, rules that must appear, rules that must not)
FIXTURES = [
    ("sloppy_en.md", [], 1, ["dash.em", "quote.emphasis", "contrast.its-not", "closer.curious"], []),
    ("sloppy_ru.md", [], 1, ["dash.em", "quote.emphasis", "contrast.ru-delo", "closer.ru-bait"], []),
    ("sloppy_uk.md", [], 1, ["dash.em", "quote.emphasis", "fragment.run"], []),
    ("clean_en.md", [], 0, [], []),
    ("clean_ru.md", [], 0, [], []),
    ("clean_uk.md", [], 0, [], []),
    ("ad_sloppy_en.md", ["--ad"], 1, ["dash.em", "inflated.core", "ad.claim", "ad.urgency"], []),
    ("ad_clean_ru.md", ["--ad"], 0, ["audience.ru-callout"], []),
    ("batch_en.md", ["--batch"], 0, ["batch.questions"], []),
]

# (inline text, extra args, expected exit code, must appear, must not appear)
GUARDS = [
    ("Pages 10–20 cover the setup.", [], 0, [], ["dash.en", "dash.em"]),
    ("Мы отправляем «Новой почтой» и «Укрпочтой».", [], 0, [], ["quote.emphasis"]),
    ("Run `make — all` and wait.", [], 0, [], ["dash.em"]),
    ("Read https://example.com/step-by-step--guide before you start.", [], 0, [], ["xbyx.en", "dash.ascii"]),
    ('She said, "I will be there by noon."', [], 0, [], ["quote.emphasis"]),
    ("The war lasted from 1941—1945.", [], 0, ["dash.range"], ["dash.em"]),
    ('<p class="lead">Opening hours changed.</p><p>We now close at six.</p>', [], 0, [], ["quote.emphasis"]),
    ("It's not cheap, but it's good.", [], 0, [], ["contrast.its-not"]),
    ("Не в субботу, а в воскресенье. Перенесли из-за дождя.", [], 0, [], ["contrast.ru-delo"]),
    ("Москва — столица России.", [], 1, ["dash.em"], []),
    ("For anyone who runs daily: 4 cleanings for the price of 3.", ["--ad"], 0, ["audience.callout"], []),
    ("For anyone who runs daily: 4 cleanings for the price of 3.", [], 1, ["audience.callout"], []),
    ("> Here's the thing: quoted text.\n\nMy answer is short.", [], 0, [], ["opener.drumroll"]),
    ("> Here's the thing: quoted text.\n\nMy answer is short.", ["--quotes"], 1, ["opener.drumroll"], []),
    ("Fast. Simple. Yours.", [], 1, ["fragment.run"], []),
    ("Fast. Simple. Yours.", ["--ad"], 0, ["fragment.run"], []),
]


def run(args):
    proc = subprocess.run([sys.executable, str(LINTER), "--json", *args],
                          capture_output=True, text=True, encoding="utf-8", timeout=60)
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        data = {"hits": [], "summary": {"hard": "?", "soft": "?"}}
    return proc.returncode, data


def verdict(label, code, data, want_code, must, must_not):
    rules = {h["rule"] for h in data["hits"]}
    problems = []
    if code != want_code:
        problems.append(f"exit {code}, expected {want_code}")
    problems += [f"missing {r}" for r in must if r not in rules]
    problems += [f"unexpected {r}" for r in must_not if r in rules]
    s = data["summary"]
    status = "PASS" if not problems else "FAIL"
    print(f"{status}  {label:<60} exit={code} hard={s['hard']} soft={s['soft']}"
          + (f"  <- {'; '.join(problems)}" if problems else ""))
    return not problems


def main():
    ok = True
    for name, args, want, must, must_not in FIXTURES:
        code, data = run([str(HERE / name), *args])
        ok &= verdict(" ".join([name, *args]), code, data, want, must, must_not)
    for text, args, want, must, must_not in GUARDS:
        code, data = run(["--text", text, *args])
        label = (text.replace("\n", " ")[:48] + " " + " ".join(args)).strip()
        ok &= verdict(label, code, data, want, must, must_not)
    print("all expectations hold" if ok else "some expectations failed")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

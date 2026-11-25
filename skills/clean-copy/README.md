# clean-copy

An agent skill that checks text before it leaves for other people: posts, replies, articles, emails, DMs, announcements, ad copy. It removes the traces of machine-written prose without flattening the author's voice and without inventing facts. English and Russian are covered by a linter and a checklist, Ukrainian by the checklist.

## Contents

- `SKILL.md`: what the agent follows. When to run, workflow, causes, rules, guard rails, scoring, output format.
- `references/checklist.md`: the review passes in order.
- `references/patterns.md`: the pattern catalogue with examples and linter rule ids.
- `references/examples.md`: before and after rewrites.
- `references/ads.md`: ad mode.
- `scripts/clean_copy_check.py`: the linter. Python 3, standard library only.
- `tests/`: sloppy and clean fixtures plus `run_tests.py`.

## Linter

```
python3 scripts/clean_copy_check.py draft.md
python3 scripts/clean_copy_check.py --text "Here's the thing: ..." --lang en
python3 scripts/clean_copy_check.py ad.txt --ad --json
python3 scripts/clean_copy_check.py week.md --batch
python3 scripts/clean_copy_check.py --list-rules
```

HARD hits must all be fixed; SOFT hits are for review. The exit code is 1 when there is at least one HARD hit, 0 otherwise, and 2 for usage or input errors. Code blocks, inline code, URLs, e-mail addresses, HTML tags and comments, YAML front matter and blockquotes (unless `--quotes`) are skipped. `--lang auto` (the default) detects the language from the letters used; Ukrainian is detected and gets only the language-neutral checks.

The linter is a cheap first pass. A clean run means nothing countable was found; the checklist does the actual review.

## Install

Copy the folder into your agent's skills directory. For Claude Code that is `.claude/skills/clean-copy/` in a project or `~/.claude/skills/clean-copy/` for all projects. Invoke it with `/clean-copy` and a text, or let the agent run it before any outward text.

## Tests

```
python3 tests/run_tests.py
```

## License

Part of Archi, released under the MIT license (see the repository's LICENSE file).

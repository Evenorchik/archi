---
name: clean-copy
description: Final quality gate for text that other people will read. Run it before you hand over or save as final any post, comment or reply, article or site page, email, DM, announcement, ad copy, or message the user will forward. It removes the traces of machine-written prose (dash habits, emphasis quotes, throat-clearing and announcement openers, 'not X but Y' reversals, rule-of-three padding, template closing questions and engagement bait, inflated, academic or business vocabulary, hedging, recaps, fake intimacy) while keeping the author's meaning, facts and voice and never inventing new ones. Russian and English with a linter, Ukrainian by checklist, plus an ad mode for advertising copy. Trigger on /clean-copy, on requests such as 'check this text' or 'make it sound less like AI', and on your own initiative whenever outward text is about to leave. Skip it for chat with the user about the work, code, commit messages, logs and internal docs.
user-invocable: true
---

# clean-copy

The last pass over any text that leaves the conversation for other readers. It strips what makes prose read as generated and nothing more: meaning, facts, register and the author's voice stay.

## When to run

Before you show a final draft, or save a file meant to be published or sent:

- posts, comments, replies and DMs on any network or forum
- articles, blog posts, site pages, newsletters
- emails: personal, outreach, announcements, follow-ups
- ad copy, store listings, landing-page blurbs (in ad mode, see below)
- any message the user will forward to someone else

Do not run it on chat with the user about the work itself, code and code comments, commit messages, logs, specs, internal notes and docs. A work question from the user gets a normal answer.

If you already handed over outward text without the gate, run it now and send the corrected version with a short list of what changed. A late fix is better than slop that stays out there.

## Languages

- **English, Russian**: linter and checklist.
- **Ukrainian**: checklist only. The linter recognizes Ukrainian and runs only its language-neutral checks (dashes, quotes, fragments, rhythm, layout). It has no Ukrainian word lists, so go through the word-level passes of the checklist by hand.
- **Other languages**: the structural passes of the checklist apply; the linter's word rules do not.

## Modes and order

- **Standard**: everything below.
- **Ad mode**: advertising copy. Read [references/ads.md](references/ads.md) and lint with `--ad`. A few patterns that are tells in a post are working tools in an ad; everything else still applies.
- **Batch**: several texts that will be seen together (a week of posts, an email sequence, replies in one thread). Check each text, then the set, and lint with `--batch`. Sameness across a set is a tell even when every text passes alone.
- **English by a non-native writer**: fix calques and non-native phrasing first, then run this gate. Making phrasing idiomatic rebuilds sentences and can bring patterns back; doing it after the gate undoes the gate.
- **Ad register** (shorter, verb-first, punchier) is tuned last, after this gate.

## Workflow

0. **Lint.** `python3 scripts/clean_copy_check.py DRAFT [--lang ru|en|auto] [--ad] [--batch]` or `--text "..."`. Fix every HARD hit. The only HARD hits you may keep are false positives you can name (a real quotation flagged as an emphasis quote, for example); list them in your report. Read every SOFT hit and decide. The linter counts, it cannot read: a clean run means nothing countable was found, not that the text is good.
1. **Write down the meaning.** Before you touch a sentence, note for yourself the one to three things the text claims, asks or tells, who reads it, and where (forum, inbox, landing page). If you can hardly fill this in, the draft is mostly padding and the result will be much shorter. That is the right outcome.
2. **Diagnose by cause.** Walk [references/checklist.md](references/checklist.md). Look for clusters, not single words: one "moreover" means nothing, while a triplet, two inflated adjectives and a closing moral in one paragraph are a pattern. Name the cause of each cluster from the table below; the cause tells you what to write instead.
3. **Rewrite from meaning, not word by word.** If a paragraph has more than three hits, or the score is under the gate, write it again from your notes and use the draft only as a source of facts. Word swaps keep the skeleton (hook, triplet, moral), and readers recognize the skeleton. A text with one or two hits and a living voice gets fixed in place.
4. **Check the guard rails**: don't overcorrect, never invent, keep the voice (below).
5. **Score** with the rubric below. Under 35 of 50, or any axis at 4 or lower: back to step 3. After two full rewrites that still miss the gate, stop and hand over the best version with the open problems named, or ask the author for the missing facts.
6. **Relint the final text.** Zero HARD hits, apart from named false positives.

## Why generated prose looks like this

Every pattern has a cause. Ban the pattern without removing the cause and a relative grows in its place: dashes turn into colons, "not X but Y" turns into "X? No. Y.", one stock verb into another. If a fix produced a sibling of the pattern, the cause is still there.

| Cause | What it produces | What to do instead |
|---|---|---|
| Reaching for the most probable phrase | stock words and metaphors, abstract nouns, generic examples | the specific: a number, a name, what happened |
| Trained to look helpful at a glance | headings, bold, bullets, triplets, recaps, cheerfulness | write for someone who reads every word; prose by default |
| Penalized for being wrong or one-sided | piles of hedges, both-sides framing, "it's important to note" | commit; keep one hedge where the doubt is real and say what is unknown |
| No stake in the outcome | everything is key, crucial, a game-changer | decide what matters and say why; let small things stay small |
| Absorbed genre templates (feed posts, SEO pages, school essays, press releases) | "In today's world", "The result?", "In conclusion", audience call-outs | notice the template and step out of it; start with the point |
| Writing sentence by sentence with no reader in mind | a moral after every paragraph, significance tails, announcements | trust the reader; end the paragraph when its content ends |
| Avoiding repetition at any cost | synonym carousels, even parallel structure, metronome rhythm | repeat the key noun; let rhythm follow content |
| The assistant role | "Great question", "Hope this helps", "Feel free to reach out" | drop the chat frame; the text is not a reply to a user |
| Translation drift, strong in Russian output | officialese (является, осуществляет), calques (вызовы, на ежедневной основе) | use the register of the venue; read it aloud |

## Core rules

The short version. The full catalogue with examples and linter ids is in [references/patterns.md](references/patterns.md).

1. **No em dashes as punctuation**, and no spaced en dash or ` -- ` standing in for one. Use a comma, a period, parentheses, or rebuild the sentence. Hyphens in compounds and dashes inside number ranges are fine. This holds for Russian as well: in casual outward text a typographic dash is one of the most reliable machine tells, and «X — это Y» reads fine as «X это Y» or as a rebuilt sentence.
2. **No quotes for emphasis**, as labels, or around concepts. Let the phrase sit in the sentence. Real quotations of someone else, and names and titles in «», are fine.
3. **No throat-clearing, drumroll or announcement openers.** Start with the point.
4. **No staged reversals** ("it's not X, it's Y", «не просто X, а Y», «дело не в X, а в Y»). Say Y. A plain factual correction ("Sunday, not Saturday") is fine.
5. **No rule-of-three padding.** Use as many items as the content has; two is often right.
6. **No template closers or engagement bait.** End on the last real point. If you need replies, ask one specific question. In a batch, at most a third of the texts end on a question.
7. **No audience call-out openers** ("For anyone who…", «Для тех, кто…», "Whether you're X or Y"). Fold the reader into the sentence. Relaxed in ad mode.
8. **No outsider disclosure as the first line** ("I'm not a doctor, but…", «Я не специалист, но…»). Lead with the observation and give your background in the second or third paragraph. If leaving it out would mislead, disclose right after the hook.
9. **Plain words** over inflated, academic or business vocabulary. Research terms belong in research and jargon in internal decks.
10. **Hedge once**, where the doubt is real, and say what is uncertain. Never stack hedges.
11. **No assistant frame, sycophancy, generic praise or fake intimacy.**
12. **No signposting, meta-commentary, recaps or morals.** The paragraph ends when its content ends.
13. **Be specific, but only with facts you have** (see Never invent).
14. **Active voice by default**: the people who act are the subjects. Keep the passive where the actor is unknown or irrelevant.
15. **One name per thing.** Pick "the app" and keep it; don't rotate app, platform, tool, solution.
16. **Plain "is" and "has"** (Russian «это», «есть», or nothing) over serves as, stands as, является, представляет собой.
17. **No significance tails** ("…, highlighting the importance of X", «…, обеспечивая бесшовный опыт»). Cut the tail, or give it its own sentence with a concrete consequence.
18. **No "X by X" promises** (step by step, week by week, шаг за шагом) unless the text really walks through each one. Name what the reader gets.
19. **No lazy absolutes.** Narrow everyone, always, никогда to what you know.
20. **Vary rhythm**: not four sentences of one length in a row, not a punchline at the end of every paragraph, not one sentence per paragraph, not a run of fragments.
21. **Formatting fits the venue.** A short post or email needs no headings, bold, emoji bullets or hashtag piles.
22. **First-person replies** (forums, comments, DMs): answer the one or two points that matter instead of walking through every point of the other person in order; vary sentence openings (a run of "The [noun]…" reads like an essay); spell out hyphenated compounds instead of sprinkling them.

## Guard rails

### Don't overcorrect

- **Density is the tell, not presence.** People write "however", use the passive, a triplet, an adverb. Leave them where they do a job. Only HARD rules are absolute.
- **A synonym swap is not a rewrite.** The reader recognizes the skeleton, not the word.
- **Don't turn removed dashes into colons.** Asides go in parentheses or between commas; a new thought gets a period. At most one "Label: reveal" colon per text.
- **Keep real caveats.** If a claim is genuinely uncertain, the caveat is information.
- **Don't chop the text into fragments** or one-line paragraphs. That is another machine signature, not a human one.
- **Don't fake a human**: no planted typos, slang for flavor, "lol", or invented vulnerability.
- **Keep register, meaning and the mandatory parts.** Legal text stays formal. Required disclaimers, terms, prices, dates and calls to action stay.
- **Keep the structure the genre needs.** A changelog needs a list; an event notice needs date, time and place.
- **Never edit quotations** of other people.
- **Plain, natural punctuation is fine.** Commas, periods, parentheses and the occasional colon are not suspicious.

### Never invent

Rule 13 is not a license to make things up. Invented numbers, stories, customer quotes, features, reasons and memories are worse than vague words, and in medical, legal or financial text they are a liability. This covers facts about the author, the company, the product, people and events. A product feature the source never mentions is invented, and so is a reason the author never gave, however plausible it sounds. Hypothetical examples framed as such, comparisons and jokes are writing, not invention.

When a vague claim can only be fixed with a fact you don't have:

- the text works without it: cut it;
- it doesn't: put a placeholder in square brackets, such as `[sign-ups in the first week]`, and write the sentence so it still reads if the author deletes the placeholder.

Placeholders only stand in for what the source implied. Facts that would help but are not in the source go in a separate "Could add" list of two or three items. Exception: genres that are useless without specifics (a personal essay, a "what I learned" post). For those, return a skeleton with placeholders or questions for the author.

### Keep the voice

You remove machine traces; you don't impose a house style. Keep the author's vocabulary, humor, sentence habits, lowercase forum style and dialect, and the register of the venue. Every fact, number, name, link and date of the source survives into the result.

## Scoring

Score the final text on five axes, 0 to 10 each.

| Axis | 9–10 | 5–6 | 0–2 |
|---|---|---|---|
| Substance | every sentence states, shows or asks something concrete | a few sentences only announce or gesture | mostly announcements and abstractions |
| Economy | nothing left to cut | a recap, a tail or some filler remains | half of it could go |
| Rhythm | sentence and paragraph lengths follow the content | stretches of metronome or staccato | uniform or chopped throughout |
| Voice | sounds like this author in this venue | correct but generic, brochure or assistant tone in places | brochure or assistant throughout |
| Reader | trusts the reader: no explaining the obvious, no permission slips, no morals | a moral or a reassurance or two | lectures and recaps |

**Fidelity** is pass/fail and sits outside the score: an invented fact, a lost fact, number, name or link, or a changed meaning fails the text whatever it scores.

**Gate**: under 35, or any axis at 4 or lower, means rewrite from step 3.

## Output when the user asks for a check

When the user sends a text to check (`/clean-copy` plus a text or a file), reply in this shape:

```
SCORE 38/50 (substance 8, economy 7, rhythm 8, voice 7, reader 8) · fidelity: pass
LINTER before: 9 HARD, 4 SOFT · after: 0 HARD, 1 SOFT (kept: real quotation, L4)

HITS
1. L1 "Here's the thing:" drumroll opener; cause: genre template. Now opens with the fact.
2. L3 em dash after "hours" → comma.
3. L9 "What do you think? I'd love to hear…" template close → ends on the open pricing question.

REWRITE
<the full corrected text>

NEEDS FROM YOU (only if placeholders remain, one line each)
- [sign-ups in the first week]: the number from your CRM

COULD ADD (optional, two or three items)
- the price of the old plan, to make the comparison concrete

CHANGES
- 3 intensifiers cut (really, just, simply)
- closing question → closing statement
```

When you run the gate on your own outward text, apply the fixes and hand over the clean version. Mention the gate only when placeholders or open problems remain.

## Files

- `scripts/clean_copy_check.py`: the linter (Python 3, standard library). `--list-rules` prints every rule with its severity.
- `references/checklist.md`: the review passes, in order.
- `references/patterns.md`: the pattern catalogue, with examples in both languages and linter rule ids.
- `references/examples.md`: before and after rewrites.
- `references/ads.md`: ad mode.
- `tests/`: sloppy and clean fixtures, `run_tests.py`.

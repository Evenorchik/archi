---
name: clean-copy
description: Final quality gate for text that other people will read. Run it before you hand over or save as final any post, comment or reply, article or site page, email, DM, announcement, ad copy, or message the user will forward. It removes the traces of machine-written prose (dash habits, emphasis quotes, throat-clearing and announcement openers, 'not X but Y' reversals, rule-of-three padding, template closing questions and engagement bait, inflated, academic or business vocabulary, hedging, recaps, fake intimacy) while keeping the author's meaning, facts and voice and never inventing new ones. Russian and English with a linter, Ukrainian by checklist, plus an ad mode for advertising copy. Trigger on /clean-copy, on requests such as 'check this text' or 'make it sound less like AI', and on your own initiative whenever outward text is about to leave. Skip it for chat with the user about the work, code, commit messages, logs and internal docs.
user-invocable: true
---

# clean-copy

The last pass over any text that leaves the conversation for other readers. It takes out what marks prose as generated and touches nothing else: meaning, facts, register and the author's voice come through intact.

## When to run

Before you show a final draft, or save a file meant to be published or sent:

- posts, comments, replies and DMs on any network or forum
- articles, blog posts, site pages, newsletters
- emails: personal, outreach, announcements, follow-ups
- ad copy, store listings, landing-page blurbs (ad mode, see below)
- anything the user will forward to someone else

Not for: chat with the user about the work itself, code and code comments, commit messages, logs, specs, internal notes and docs. Answer the user's work questions in your normal voice.

Forgot the gate on something already sent? Run it now and follow up with the corrected text and the list of changes.

## Languages

- **English, Russian**: linter and checklist.
- **Ukrainian**: checklist only. The linter recognizes Ukrainian and then runs just its language-neutral checks (dashes, quotes, fragments, rhythm, layout). It has no Ukrainian word lists, so the word-level passes of the checklist are done by hand.
- **Other languages**: the structural passes of the checklist apply; the linter's word rules do not.

## Modes and order

- **Standard**: everything below.
- **Ad mode**: advertising copy. Read [references/ads.md](references/ads.md) and lint with `--ad`. A few devices that betray a post are legitimate in an ad; the rest of the gate is unchanged.
- **Batch**: several texts that will be seen together (a week of posts, an email sequence, replies in one thread). Check each one, then the set, and lint with `--batch`. A set can fail while each member passes: ten posts that all close on a question read as one template.
- **With a language pass**: when an English draft comes from a non-native writer, do the idiom pass before this gate, never after. Idiom fixes rewrite sentences, and anything rewritten after the gate is unchecked.
- **Ad register** (shorter, verb-first) is tuned last, after the gate.

## Workflow

0. **Lint.** `python3 scripts/clean_copy_check.py DRAFT [--lang ru|en|auto] [--ad] [--batch]`, or `--text "..."`. Fix every HARD hit. The only HARD hit you may leave is a false positive you can name, such as a genuine quotation caught by the emphasis-quote rule; say so in your report. Read every SOFT hit and decide. The linter counts and cannot read, so a clean run only means nothing countable turned up.
1. **State the message.** In a scratch note, finish three sentences: "After reading, the reader knows …", "The reader is asked to …", "The reader is …, reading this in …". Thin answers mean a padded draft. Expect the rewrite to lose a lot of length, and let it.
2. **Diagnose by cause.** Walk [references/checklist.md](references/checklist.md) and judge paragraphs, not words. A single flagged word is noise. A paragraph that stacks several families (an opener, a triplet, a tail, a moral) was produced by a template, and patching one word will not change that. For each such paragraph, find the cause in the section below; it tells you what to write instead.
3. **Repair or rebuild.** Repair in place when the text has a voice of its own and only scattered hits. Rebuild when a paragraph carries more than three hits or the score misses the gate: put the draft aside, write from the scratch note, then go back to the draft only to collect its facts. Sentence-level edits preserve the draft's architecture, and the architecture is what gives it away.
4. **Check the guard rails** below: overcorrection, invention, voice.
5. **Score** with the rubric below. Under 35 of 50, or any single axis at 4 or lower, sends you back to step 3. If two full rebuilds still miss the gate, stop: hand over the best version with the remaining problems named, or ask the author for the facts that are missing.
6. **Relint the final text**: no HARD hits left except named false positives.

## Where the patterns come from

Knowing the source of a pattern tells you what to write in its place. Suppressing the symptom only moves it: strip the em dashes and semicolons multiply, strip "Here's the thing" and "The thing is" appears, strip one hype verb and the next one from the thesaurus arrives. When the replacement belongs to the same family, the source is still active.

- **Averaging.** A model continues with the most typical phrase, so drafts converge on the median sentence: stock metaphors, abstract nouns, adjectives that fit any product. Ask what only this author, writing about this case, would put here, then supply that particular: a figure, a date, a proper noun, an observed detail.
- **Rewarded for the skim.** Outputs are judged at a glance, which pays for visible structure, enthusiasm and closure: headings in short texts, bold, bullets, triplets, summaries, an upbeat tone. Assume a reader who reads to the end, and add structure only when the content has that shape.
- **Risk aversion.** Being wrong is punished, so every claim gets padded with qualifiers, balance and disclaimers. Decide what you believe; mark only the specific thing you are unsure of.
- **No point of view.** With nothing at stake, every item gets the same inflated weight. Rank the points: the top one earns the emphasis, the rest get plain statements.
- **Borrowed genre.** Feed posts, SEO pages, school essays and press releases lend their openers, reveals and closers. Name the genre the draft borrowed from, then write in the genre it actually is: a reply is a reply, an email is an email.
- **Sentence-by-sentence production.** Each sentence is written to stand alone, so each paragraph gets its own setup and its own lesson, and finished sentences grow tails explaining why they matter. Treat a paragraph as one move and stop after its last piece of content.
- **Variety enforcement.** Repetition is penalized, which produces synonym rotation, even parallelism and a regular beat. Repeating the key term is clarity; sentence length follows the thought.
- **Service persona.** The model is built to assist, so its prose greets, praises the question, offers more help and hopes it helped. Write as the sender, to this recipient, about the matter at hand.
- **Register drift**, strongest in Russian output: official-notice vocabulary (является, осуществляет, в рамках) and word-for-word English (вызовы, на ежедневной основе). Say it the way you would say it to a neighbour, then check that the verb is a real verb.

## Core rules

The short form. The catalogue with examples and linter ids is in [references/patterns.md](references/patterns.md).

1. **No em dashes as punctuation**, and no spaced en dash or ` -- ` standing in for one. Read the sentence without the dash: one thought means a comma or nothing, two thoughts mean two sentences, a detour means parentheses or a cut. Hyphens in compounds and dashes in number ranges are fine. This applies to Russian too; in casual outward text the typographic dash is among the most reliable machine tells, and «X — это Y» works as «X это Y» or as a rebuilt sentence.
2. **No quotes for emphasis**, as labels or around concepts. Write the phrase as part of the sentence. Genuine quotations, and names and titles in «», stay.
3. **No throat-clearing, drumroll or announcement openers.** The first sentence carries content.
4. **No staged reversals** ("it's not X, it's Y", «не просто X, а Y», «дело не в X, а в Y»). Keep only the positive claim. Correcting a fact ("Sunday, not Saturday") is not a reversal.
5. **No rule-of-three padding.** As many items as the content has; often two.
6. **No template closers or engagement bait.** Stop after the last real point. If you want answers, ask one specific question in the body. In a batch, no more than a third of the texts end on a question.
7. **No audience call-out openers** ("For anyone who…", «Для тех, кто…», "Whether you're X or Y"). Address the reader directly or describe their situation. Relaxed in ad mode.
8. **No outsider disclosure as the opening line** ("I'm not a doctor, but…", «Я не специалист, но…»). Open with what you noticed or want to know; your credentials, or the lack of them, can come once the reader is interested. When omitting the disclosure would mislead, keep it, just not first.
9. **Plain words** over inflated, academic or business vocabulary. Research terms belong in research and jargon in internal decks.
10. **One hedge, on the real doubt**, saying what is uncertain. Never stacked.
11. **No assistant frame, sycophancy, generic praise or fake intimacy.**
12. **No signposting, meta-commentary, recaps or morals.** Stop when the content runs out; don't append the lesson.
13. **Specific, but only with facts you have** (see Never invent).
14. **The actor is the subject**: "the council closed the road", not "the road was closed", unless nobody knows or cares who acted.
15. **One name per thing.** If it is "the bakery" in line one, it is not "the shop", "the business" and "the brand" later.
16. **Plain "is" and "has"** (Russian «это», «есть», or no verb) over serves as, stands as, является, представляет собой.
17. **No significance tails** ("…, reflecting our deep commitment to customers", «…, открывая новые горизонты»). Cut the tail; if it hid a real point, write that point as its own sentence with something checkable in it.
18. **No "X by X" promises** (step by step, day by day, шаг за шагом) unless the text delivers each step. Say what the reader gets instead.
19. **No lazy absolutes.** Shrink everyone, always, никогда to what you actually know.
20. **Rhythm follows thought**: a long sentence for a chain of reasons, a short one for a verdict. Watch for runs of equal length, paragraphs that all end on a zinger, one-sentence paragraphs all the way down, and strings of fragments.
21. **Formatting fits the venue.** A short post or email needs no headings, bold, emoji bullets or hashtag piles.
22. **First-person replies** (forums, comments, DMs): respond to the one or two points that matter rather than giving each point of the other person its own paragraph; vary sentence openings, since a run of "The [noun]…" reads like an essay; spell out hyphenated compounds instead of sprinkling them.

## Guard rails

### Don't overcorrect

- **Frequency and company, not occurrence.** Humans use every one of these devices sometimes. One passive sentence, one triplet or one "however" proves nothing. HARD rules are the only absolutes.
- **Word-by-word replacement is not repair.** Replacing words one at a time leaves the draft's skeleton standing, and the skeleton is what readers recognize.
- **Don't trade dashes for colons and semicolons.** Decide per sentence: an aside (parentheses or commas), a new thought (a period), or a list (a colon belongs there). Colons that set up a reveal ("The lesson: …") appear once per text at most.
- **Real uncertainty stays.** A caveat that reflects a genuine unknown is information; keep it and make it precise ("we haven't tested it on Windows").
- **Short is not the same as human.** Chopping prose into fragments or one-line paragraphs swaps one machine signature for another.
- **No staged authenticity**: no planted typos, forced slang, invented confessions, or emoji for warmth.
- **Register and obligations survive.** A contract clause or a terms page keeps its formal register; required disclaimers, prices, dates and calls to action are not yours to cut.
- **Keep the structure the genre needs.** A changelog needs a list; an event notice needs date, time and place.
- **Never edit quotations** of other people.
- **Ordinary punctuation is fine.** Commas, periods, parentheses and the occasional colon are not suspicious, and neither is a plain sentence.

### Never invent

Specific beats vague only when the specifics are true. A made-up figure, anecdote, testimonial, product capability, motive or memory does more damage than the vague phrase it replaced, and in health, legal or money matters it can hurt someone. The test: every factual statement in your version traces back to the source or to the author. That includes things that merely sound likely, such as a capability the product probably has or a reason the author probably had. Writing devices are not facts: a clearly hypothetical case ("imagine a bike that has stood in the hallway since spring"), an analogy or a joke is fine.

For a vague statement that only a fact could repair:

1. Ask whether the text loses anything without it. If not, delete it.
2. If it does, leave a gap in square brackets that names what belongs there, such as `[delivery time after the change]`, and build the sentence so it still stands if the author deletes the bracket.

Gaps mark only what the source already pointed at. Ideas for facts the source never mentioned go under the text as "Could add", two or three at most. Some genres collapse without specifics (a personal essay, a lessons-learned post); when the facts are missing there, hand back a skeleton with gaps or a short list of questions for the author instead of a finished piece.

### Keep the voice

You remove machine traces; you do not impose a house style. The author's vocabulary, humour, sentence habits, lowercase forum style and dialect stay, and so does the register of the venue. Trace the facts both ways: every fact, number, name, link and date of the source appears in the result.

## Scoring

Score the final text on five axes, 0 to 10 each.

| Axis | 9–10 | 5–6 | 0–2 |
|---|---|---|---|
| Substance | every sentence states, shows or asks something concrete | a few sentences only announce or gesture | mostly announcements and abstractions |
| Economy | nothing left to cut | a recap, a tail or some filler remains | half of it could go |
| Rhythm | sentence and paragraph lengths follow the content | stretches of metronome or staccato | uniform or chopped throughout |
| Voice | sounds like this author in this venue | correct but generic, brochure or assistant tone in places | brochure or assistant throughout |
| Reader | trusts the reader: nothing obvious explained, no reassurance, no lesson | a lesson or a reassurance or two | lectures and recaps |

**Fidelity** is pass or fail, outside the score: an invented fact, a lost fact, number, name or link, or a shifted meaning fails the text whatever its score.

**Gate**: under 35, or any axis at 4 or lower, means rebuild from step 3.

## Output when the user asks for a check

When the user sends a text to check (`/clean-copy` with a text or a file), answer in this shape:

```
SCORE 37/50 (substance 8, economy 7, rhythm 7, voice 7, reader 8) · fidelity: pass
LINTER before: 18 HARD, 3 SOFT · after: 0 HARD, 1 SOFT (kept: genuine quotation, L6)

HITS
1. L3 "In today's fast-paced digital landscape" stock scene-setting; cause: borrowed genre. Opens on the client now.
2. L5 "The result? Two of them…" staged reveal → "Two of the three agreed to the higher rate."
3. L17 "In conclusion… What do you think? I'd love to hear…" recap plus template close → ends on the rate itself.

REWRITE
<the full corrected text>

NEEDS FROM YOU (only if gaps remain, one line each)
- [delivery time after the change]: from the logistics report

COULD ADD (optional, two or three items)
- the old rate next to the new one

CHANGES
- three-item list of slogans folded into one sentence
- reversal "it's not about the rate" removed, the claim stated directly
```

When you run the gate on your own outward text, apply the fixes and hand over the clean version. Mention the gate only when gaps or open problems remain.

## Files

- `scripts/clean_copy_check.py`: the linter (Python 3, standard library). `--list-rules` prints every rule with its severity.
- `references/checklist.md`: the review passes, in order.
- `references/patterns.md`: the pattern catalogue with examples in both languages and linter rule ids.
- `references/examples.md`: before and after rewrites.
- `references/ads.md`: ad mode.
- `tests/`: sloppy and clean fixtures and `run_tests.py`.

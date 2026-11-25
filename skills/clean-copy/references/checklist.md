# Review checklist

Go through every pass in order. A hit means a fix, not "probably fine". Linter rule ids are in brackets where the linter helps; where there are none, only reading finds it.

## Pass 0. Meaning first

- [ ] The scratch note is done: what the reader should know afterwards, what they are asked to do, who they are and where they read it.
- [ ] Paragraphs with more than three hits get rebuilt from the note, not patched.
- [ ] Each cluster of hits has a named cause from the table in SKILL.md.

## Pass 1. Punctuation and words

- [ ] Em dashes, spaced en dashes, ` -- ` `[dash.em, dash.en, dash.ascii]` → comma, period, parentheses, or a rebuilt sentence.
- [ ] Quotes around a short phrase for emphasis, as a label, or around a concept `[quote.emphasis]` → drop the quotes.
- [ ] Throat-clearing, drumroll and hook openers `[opener.*]` → start with the point.
- [ ] Announcements and meta-commentary `[signpost.*]` → cut; the next sentence is the start.
- [ ] Emphasis crutches and permission slips `[crutch.*]`: "Let that sink in", "Read that again", "And that's okay", «Вдумайтесь», «И это нормально» → cut.
- [ ] Inflated vocabulary and hype verbs `[inflated.*]`: delve, tapestry, unlock potential, game-changer, «погрузиться в мир», «раскрыть потенциал», «на новый уровень» → plain words.
- [ ] Fashionable decorations `[trend.*]`: quietly, load-bearing, doing the heavy lifting, a masterclass in, «тихая революция», «это база» → drop the ornament, state the claim.
- [ ] Vague declaratives and telling instead of showing `[vague.*]`: "the possibilities are endless", "this changes the game", «будущее уже наступило» → name the consequence or cut.
- [ ] Business jargon `[jargon.*]` → the plain action.
- [ ] Research vocabulary in a casual text `[academic.*]` → everyday words.
- [ ] Russian officialese and calques `[officialese.ru, calque.ru]` → plain verbs, «это», «есть».
- [ ] Copula dodges `[copula.dodge]` → is, has.
- [ ] Significance tails: "…, highlighting", "…, ensuring", «…, подчёркивая», «…, что делает его» → cut, or a separate sentence with a concrete consequence.
- [ ] Hedges `[hedge.*, hedges.*]`: stacked, piled up, or hiding who holds the view → one hedge where the doubt is real.
- [ ] Intensifier piles `[intensifiers.*]` → keep only those that change the meaning.
- [ ] Generic praise `[praise.*]` → the detail that earned it.
- [ ] Absolutes `[absolutes.*]` → narrow to what you know.
- [ ] Fake intimacy `[intimacy.*]`: "trust me", "between you and me", «открою секрет» → keep only if literally true.
- [ ] Assistant frame and form-letter formulas `[assistant.*]`: "Great question", "Hope this helps", «Надеюсь, это поможет» → delete.

## Pass 2. Sentence and paragraph structure

- [ ] Staged reversals `[contrast.*]`: "It's not X. It's Y.", "X isn't the problem. Y is.", «Это не X, это Y», «Дело не в X, а в Y» → keep only the positive claim.
- [ ] Negative build-up `[contrast.negative-buildup]`: "Not X. Not Y. Z." → lead with Z.
- [ ] Fragment runs and staccato `[fragment.*]`: "Bold. Local. Honest." → one sentence that says how they relate.
- [ ] Staged reveals `[reveal.*]`: "The result? …", «Итог? …», "Why? Because…" → state it. At most one "Label: reveal" colon per text.
- [ ] Rhetorical hooks `[opener.hook, opener.wondered]`: "What if I told you", "Picture this", «Задумывались ли вы» → say it.
- [ ] Audience call-outs `[audience.*]` → fold the reader into the sentence (relaxed in ad mode).
- [ ] Outsider disclosure in the first lines `[outsider.*]` → move it to paragraph two or three.
- [ ] False agency and hidden actors `[agency.*, passive.en]`: "the numbers speak for themselves", "a decision was made", «было принято решение» → name who did it.
- [ ] Lecturing from above: claims about "people", "society" or "many of us" where the text could speak to the reader's own case («Многие из нас…», «Общество диктует…») → talk about the case in front of you.
- [ ] One thing under several names `[names.cycling]` → one name, repeated.
- [ ] A moral or recap at the end of a paragraph or of the text `[recap.*]`: "Ultimately…", "That's the real lesson here", «Вот и весь секрет» → end on the last fact or the ask.
- [ ] "What makes X special is…" wind-ups `[cleft.en]` → start with the thing itself.
- [ ] "X by X" promises `[xbyx.*]` → keep only if the text walks through each one.
- [ ] Template closers and engagement bait `[closer.*]`, stacked either/or questions at the end `[closer.stacked]` → end on a statement, or one specific question.

## Pass 3. Rhythm and layout

- [ ] Four or more sentences of nearly one length in a row `[rhythm.metronome]`.
- [ ] Paragraphs that keep ending on a short punchline `[rhythm.punchlines]`.
- [ ] One sentence per paragraph throughout `[rhythm.one-line-paragraphs]`.
- [ ] Triplets everywhere `[rhythm.triplets]`, parallel triplets "the one who…, the one who…" `[rhythm.parallel]`.
- [ ] A question asked and answered in the next breath.
- [ ] Lists: bold labels, items of one length, the same first word, emoji bullets `[list.*]`.
- [ ] Headings, bold, emoji, hashtags and exclamation marks fit the venue `[layout.*]`.

## Pass 4. Voice and venue

- [ ] It sounds like this author in this venue, not like a marketer or an assistant.
- [ ] A reply answers the one or two points that matter. It does not cover every point of the other person, one paragraph each.
- [ ] Sentence openings vary; not a run of "The [noun]…" `[rhythm.the-openers]`.
- [ ] Hyphenated compound modifiers are few and standard ones (follow-up, well-known) `[rhythm.hyphen-compounds]`.
- [ ] Batch: openings differ, no shared template, at most a third of the texts end on a question `[batch.*]`.
- [ ] Ukrainian: do Pass 1 and Pass 2 by hand, the linter has no Ukrainian word lists. Typical phrases: «варто зазначити», «у сучасному світі», «відіграє ключову роль», «є невід'ємною частиною», «не просто X, а Y», «Давайте розберемося», «Підсумовуючи», «Сподіваюся, це допоможе», «Пишіть у коментарях», «зануритися у світ», «вивести на новий рівень». Russianisms such as «являється» (use «є»), «приймати участь» («брати участь»), «на протязі» («протягом») are errors, not style.

## Pass 5. Overcorrection and invention

- [ ] A sibling took the place of a removed pattern (dash → semicolon or colon, "Here's the thing" → "The thing is", one hype verb → its synonym)? Rebuild the whole sentence.
- [ ] More than one "Label: reveal" colon?
- [ ] Chopped into fragments or one-line paragraphs?
- [ ] Did a genuine uncertainty disappear? Restore it, stated precisely.
- [ ] Put source and result side by side and trace every factual statement of the result back to the source. Anything that does not trace (a figure, an anecdote, a quote, a capability, a motive) goes, or becomes a `[gap]`.
- [ ] Trace the other way too: every figure, name, link, date, price and mandatory disclaimer of the source appears in the result.
- [ ] The author's voice and register are still there.

## Pass 6. Score and relint

- [ ] Scored with the rubric in SKILL.md. Under 35, or any axis at 4 or lower: rewrite.
- [ ] Relinted: zero HARD hits, or only named false positives.

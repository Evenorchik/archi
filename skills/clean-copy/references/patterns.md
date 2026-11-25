# Pattern catalogue

Families of machine-sounding prose, each with the reason it reads as generated, examples in English and Russian, the fix, and the linter rules that catch it (`H` = HARD, `S` = SOFT, `ad→S` = SOFT in ad mode). `python3 scripts/clean_copy_check.py --list-rules` prints the live list.

The examples are illustrations, not exhaustive lists. A phrase that is missing here but does the same job is the same pattern.

## 1. Dashes

Why: the em dash is the most frequent punctuation habit of generated text in both languages, used for asides, reveals and dramatic pauses. In casual writing people mostly type commas, periods, parentheses or a plain hyphen.

- "The update — finally — fixes sync." / «Обновление — наконец-то — чинит синхронизацию.»
- spaced en dash between words: "fast – and cheap"; ASCII stand-in: "fast -- and cheap"

Fix: a light pause is a comma, a hard break is a period, an aside goes in parentheses; otherwise rebuild the sentence. Don't swap every dash for a colon.

Not a hit: hyphens in compounds (follow-up, кто-то), number ranges (10–20, 1941—1945 is SOFT only), a dash used as a list bullet (SOFT).

Linter: `dash.em` H, `dash.en` H, `dash.ascii` H, `dash.range` S, `dash.bullet` S, `dash.hyphens` S (three or more spaced hyphens in one text, the usual result of find-and-replace on em dashes).

## 2. Emphasis quotes

Why: wrapping a short phrase in quotes marks it as a concept or a label. People let the phrase sit in the sentence.

- They call it the "right way" to onboard. / Мы делаем ремонт «под ключ» и «с душой».

Fix: remove the quotes; if the phrase needs them to make sense, it needs rewording. Real quotations of a person or a document stay (name them as false positives), and so do names and titles: «Новая почта», "The Hobbit".

Linter: `quote.emphasis` H (one to five words, starting with a lowercase letter, in "", “”, «», „“ or '').

## 3. Openers: throat-clearing, drumrolls, hooks, scene-setting

Why: the sentence warms up instead of saying something. Genre templates from feed posts, SEO pages and school essays.

- Drumroll: "Here's the thing:", "Here's why…", "The truth is…", "Let's be honest", "Let me be clear", "Can we talk about…"
- Hook: "Picture this", "What if I told you…", "Ever wondered…", "We've all been there", "It's no secret that…"
- Scene-setting: "In today's fast-paced world", "In an era of…", "In the ever-evolving landscape of…"
- RU: «Если честно,», «Честно говоря,», «Скажу прямо:», «Вот в чём дело:», «Давайте разберёмся», «Давайте начистоту», «Не буду ходить вокруг да около», «Ни для кого не секрет, что», «В современном мире», «В эпоху цифровых технологий», «Задумывались ли вы когда-нибудь…»

Softer (SOFT): "Honestly,", "It turns out", "So,", "Look,", "To my surprise", «На самом деле,», «Дело в том, что», «Итак,», «Представьте себе».

Fix: delete the opener; the claim that followed it is the start.

Linter: `opener.drumroll` H, `opener.truth` H, `opener.hook` H, `opener.wondered` H ad→S, `opener.era` H, `opener.ru-sincerity` H, `opener.ru-throat` H, `opener.ru-hook` H, `opener.ru-wondered` H ad→S, `opener.ru-era` H, `opener.preview` S, `opener.sincerity` S, `opener.spoken` S, `opener.ru-spoken` S.

## 4. Signposting and meta-commentary

Why: the text describes its own structure instead of delivering it.

- "Let me walk you through…", "Let's dive in", "Let's break it down", "In this post I'll…", "Without further ado", "Buckle up"
- labels: "Spoiler:", "Pro tip:", "Hot take:", "Fun fact:"; asides: "More on that below", "But that's a story for another day"
- RU: «В этой статье мы рассмотрим…», «Ниже расскажу…», «Сейчас объясню», «Разложу по полочкам», «Давайте рассмотрим», «Об этом ниже», «Но это уже другая история», «Спойлер:», «Лайфхак:»

Fix: cut it. If the structure is not obvious, fix the structure.

Linter: `signpost.announce` H, `signpost.ado` H, `signpost.ru-announce` H, `signpost.aside` S, `signpost.ru-aside` S.

## 5. Staged reversals and negative build-ups

Why: a decoy is set up to be knocked down, which manufactures insight. The most recognizable skeleton of generated text.

- "It's not about the money. It's about freedom." / "This isn't just a tool, it's a partner." / "The problem isn't X. It's Y." / "Price isn't the problem. Trust is." / "Not because X. Because Y."
- build-up: "Not a course. Not a webinar. A community."
- SOFT scaffolding: "not just X but also Y", "less about X and more about Y", "stops being X and starts being Y", "a feature, not a bug"
- RU: «Это не просто кофе, это ритуал.», «Не просто X, а Y», «Дело не в цене, а в доверии», «Речь не о деньгах, а о свободе», «Это не про скорость, а про качество», «Не курс. Не вебинар. Сообщество.»

Fix: say Y and, if it matters, why. A plain correction of a fact ("Sunday, not Saturday", «не в субботу, а в воскресенье») is not this pattern.

Linter: `contrast.its-not` H, `contrast.isnt-just` H, `contrast.problem-isnt` H, `contrast.not-because` H, `contrast.ru-eto-ne` H, `contrast.ru-ne-prosto` H, `contrast.ru-delo` H, `contrast.negative-buildup` H, `contrast.not-just` S, `contrast.ru-soft` S, `contrast.ru-ne-a` S.

## 6. Staged reveals and colon reveals

Why: a question is asked only to be answered in the next breath; a one-word label and a colon announce a punchline.

- "The result? Sales doubled." / "Why? Because nobody asked." / "The lesson: ship early."
- RU: «Итог? Продажи выросли вдвое.», «Почему? Потому что…», «Секрет прост: …», «Вывод: …»

Fix: state the result in one sentence ("Sales doubled after we…"). At most one label-colon per text.

Linter: `reveal.qa` H, `reveal.ru-qa` H, `reveal.colon` S, `reveal.ru-colon` S.

## 7. Fragments and staccato

Why: one-word sentences in a row fake profundity ("Speed. Quality. Price.") and replace the sentence that would say how the things relate.

- "Fresh. Local. Yours." / «Быстро. Качественно. Недорого.»

Fix: one normal sentence. In ad mode slogans are allowed (SOFT), but they still need to say something.

Linter: `fragment.run` H ad→S (three or more one- or two-word sentences), `fragment.staccato` S (four or more sentences of four words or fewer).

## 8. Rule of three and parallel triplets

Why: three items feel complete, so generated text produces three adjectives, three benefits and three examples whether or not there are three.

- "fast, reliable, and secure" in every paragraph; "the one who plans, the one who builds, the one who ships"
- RU: «быстро, удобно и надёжно», «тот, кто мечтает, тот, кто делает, тот, кто…»

Fix: as many items as the content has. One strong item beats three generic ones.

Linter: `rhythm.triplets` S (several "A, B and C" lists), `rhythm.parallel` S.

## 9. Template closers and engagement bait

Why: every post ends with a ritual invitation to comment; across several posts it becomes a signature.

- "What do you think?", "Thoughts?", "Agree?", "I'd love to hear how you…", "I'm curious what others…", "Let me know in the comments", "Share your experience below", "Am I the only one?", "Follow for more", "Tag someone who…", "If you found this useful…"
- compound either/or closers: "Did the change hit you all at once or creep up over months? And did you mention it to your team, or keep it to yourself?"
- RU: «А как у вас?», «Что думаете?», «Согласны?», «Пишите в комментариях», «Делитесь опытом», «Мне интересно, как у других», «Буду рад услышать ваше мнение», «Ставьте лайк», «Подписывайтесь», «Сохраняйте, чтобы не потерять»

Fix: end on the last real point, or on what you still don't know. If you need answers, ask one question that only these readers can answer, inside the text rather than as a ritual at the end. In a batch, at most a third of the texts end on a question.

Linter: `closer.curious` H ad→S, `closer.bait` H ad→S, `closer.tag-question` H ad→S, `closer.ru-bait` H ad→S, `closer.ru-curious` H ad→S, `closer.ru-wonder` H ad→S, `closer.ru-tag-question` H ad→S, `closer.anyone-else` S, `closer.ru-anyone` S, `closer.question` S, `closer.stacked` S.

## 10. Audience call-outs

Why: "For anyone who…" is ad targeting. In a post or a reply people fold the audience into the sentence.

- "For anyone who struggles with sleep, here's…", "If you're someone who…", "This post is for you if…", "Whether you're a beginner or a pro,", "from beginners to experts"
- RU: «Для тех, кто только начинает:», «Если вы из тех, кто…», «Неважно, новичок вы или профи,», «Будь вы студентом или пенсионером», «от новичков до профессионалов»

Fix: ask them directly ("Did your sleep get worse after…") or describe what happened to them. In ad mode the call-out is a legitimate tool (SOFT).

Linter: `audience.callout` H ad→S, `audience.whether` H ad→S, `audience.ru-callout` H ad→S, `audience.ru-whether` H ad→S, `audience.soft` S, `audience.ru-soft` S.

## 11. Outsider disclosure as the first line

Why: "I'm not one of you" as the opening tells the reader to leave before the point arrives. The position has to be earned by the post.

- "I'm not a nurse, but…", "I'm no expert, but…", "As an outsider…", "I don't usually post here, but…"
- RU: «Я не врач, но…», «Я не специалист, но…», «Пишу со стороны», «Я тут новенький, но…», «Не претендую на истину, но…»

Fix: lead with the observation or the question; give your background in paragraph two or three, where it serves the question. If leaving it out would mislead, disclose right after the hook.

Linter: `outsider.open` H, `outsider.ru-open` H, `outsider.ru-maybe` S (first two sentences only).

## 12. Assistant frame, form letters, sycophancy

Why: the text is written as a reply to a user rather than as a message from a person.

- "Great question!", "You're absolutely right", "Certainly!", "I hope this helps", "I hope this email finds you well", "As an AI…"
- SOFT formulas: "Feel free to…", "Don't hesitate to reach out", "Let me know if you have any questions", "I'd be happy to…", "Just following up", "Thanks for sharing"
- RU: «Отличный вопрос!», «Вы абсолютно правы», «Надеюсь, это поможет», «Надеюсь, письмо застанет вас в добром здравии», «Не стесняйтесь обращаться», «Если остались вопросы, пишите», «Буду рад помочь»

Fix: delete the frame; answer, ask or tell. Keep a formula only if this author would really say it.

Linter: `assistant.frame` H, `assistant.praise` H, `assistant.yes` H, `assistant.ru-frame` H, `assistant.ru-praise` H, `assistant.formula` S, `assistant.ru-formula` S.

## 13. Inflated vocabulary and hype

Why: the probable word for "important" is always the biggest one. A few words are almost exclusive to generated text.

- HARD core: delve, tapestry, a testament to, beacon of, embark on, ever-evolving, in the realm of, unlock (your full) potential, harness the power, unleash, game-changer, revolutionize the way, navigate the complexities, paradigm shift, rich tapestry, take it to the next level, elevate your…, supercharge
- SOFT: seamless, robust, cutting-edge, world-class, holistic, pivotal, myriad, meticulous, underscore, showcase, foster, leverage, empower, transformative, groundbreaking, journey, landscape, vibrant, crucial, "thrilled to announce"
- RU HARD: «погрузиться в мир/атмосферу», «окунуться в атмосферу», «раскрыть потенциал», «на новый уровень», «откройте для себя», «трудно переоценить»
- RU SOFT: «уникальный», «инновационный», «неотъемлемая часть», «играет ключевую роль», «в самом сердце», «широкий спектр», «незабываемые впечатления», «идеальное решение», «ключевой», «важнейший»
- filler that announces importance: "It's important to note", "It's worth noting", "When it comes to", "At its core", «Важно отметить», «Стоит отметить», «Когда речь идёт о», «На сегодняшний день», «По сути»

Fix: say what actually happens, in the word you would use out loud. "Unlock the potential of your data" is "see which customers order twice".

Linter: `inflated.core` H, `inflated.ru-core` H, `filler.importance` H, `filler.ru-importance` H, `inflated.soft` S, `inflated.ru-soft` S, `filler.phrase` S, `filler.ru-phrase` S.

## 14. Fashionable decorations

Why: phrases that spread through feeds and model outputs in recent years and now mark text as templated.

- quietly revolutionizing, the quiet power of, load-bearing (figurative), doing the heavy lifting, the honest answer, a masterclass in, lives rent-free
- RU: «тихая революция», «тихо меняет», «честный ответ», «это база», «мастхэв»

Fix: drop the ornament, state the claim.

Linter: `trend.en` S, `trend.ru` S.

## 15. Vague declaratives, telling instead of showing

Why: the sentence says that something is big, deep or important and stops there.

- "The implications are significant.", "The stakes are high.", "This is bigger than it seems.", "This is what leadership actually looks like.", "This really matters."
- RU: «Последствия серьёзные.», «Ставки высоки.», «Всё не так просто, как кажется.», «Вот как выглядит настоящее лидерство.», «Почему это важно?»

Fix: name the consequence ("If the API changes, every integration breaks on the same day") or cut the sentence.

Linter: `vague.en` S, `vague.ru` S.

## 16. Research vocabulary in casual text

Why: in a personal post, analyst terms read as an imitation of an analyst. Fine in an actual study.

- HARD: n=1, sample size of one, anecdata, «выборка из одного человека»
- SOFT: statistically significant, confirmation bias, control group, methodology, data points, empirically, paradigm, cognitive load; «статистически значимо», «контрольная группа», «методология», «когнитивное искажение», «парадигма», «корреляция», «репрезентативный»

Fix: "just my experience", "I didn't compare it with anything", "might be reading too much into it", «у меня так», «ни с чем не сравнивал».

Linter: `academic.posture` H, `academic.terms` S, `academic.ru-terms` S.

## 17. Business jargon

Why: corporate phrases stand in for the action they describe.

- circle back, touch base, move the needle, low-hanging fruit, deep dive, double down, lean into, synergy, bandwidth, actionable insights, pain points, learnings, going forward, on the same page, level up
- RU: «синергия», «прокачать», «драйвить», «закрыть вопрос», «новые вызовы», «точки роста», «боли клиента», «в моменте», «инсайты», «фидбек», «зона комфорта», «лучшие практики»

Fix: the plain action: "call you on Friday", "fix the signup form", «созвонимся в пятницу».

Linter: `jargon.en` S, `jargon.ru` S.

## 18. Russian officialese and calques

Why: generated Russian drifts into the register of official notices and into word-for-word English.

- officialese: «является», «представляет собой», «выступает в роли», «осуществляет», «производится оплата», «в рамках», «в целях», «посредством», «данный продукт», «в связи с тем, что», «имеет место»
- calques: «на ежедневной основе», «сделать разницу», «рады сообщить», «с гордостью представляем», «Это имеет смысл.», «дайте знать», «чувствуйте себя свободно»

Fix: plain verbs and «это»: «мы доставляем», «оплата на месте», «каждый день», «у нас новость».

Linter: `officialese.ru` S, `calque.ru` S.

## 19. Copula dodges

Why: "is" and "has" feel too plain, so the text "serves as", "stands as", "boasts".

- serves as, stands as, functions as, boasts, represents a significant…; RU: «является», «представляет собой», «служит в качестве»

Fix: is, has; «это», «есть», or no verb at all («Офис на Лесной, 8»).

Linter: `copula.dodge` S, `officialese.ru` S.

## 20. Significance tails

Why: the sentence is finished, then a participle clause explains why it matters.

- "We added reminders, ensuring a seamless experience." / "The team grew to 40, highlighting our commitment to quality."
- RU: «Мы добавили напоминания, обеспечивая бесшовный опыт.», «…, подчёркивая важность…», «…, что делает его незаменимым»

Fix: cut the tail. If it carried a real thought, give that thought its own sentence with a concrete consequence ("Reminders go out at 9 if a dose is not logged").

Linter: `tail.en` S, `tail.ru` S. Many participle clauses are legitimate, so these stay SOFT.

## 21. Hedging

Why: trained to avoid being wrong, generated text stacks qualifiers until nothing is claimed.

- stacked: "could potentially", "may possibly", "perhaps might"; RU: «возможно, может быть», «вероятно, возможно»
- voiceless: "some might argue", "arguably", "it could be argued"; RU: «кто-то скажет», «можно поспорить», «не исключено, что»
- piles: perhaps, somewhat, relatively, in some ways, to some extent, kind of; RU: «в какой-то мере», «в некотором смысле», «отчасти», «достаточно», «довольно»

Fix: commit. Keep one hedge where the doubt is real and say what is unknown ("we don't know yet whether it holds after the second month").

Linter: `hedge.double` H, `hedge.ru-double` H, `hedge.voiceless` S, `hedge.ru-voiceless` S, `hedges.en` S, `hedges.ru` S (three or more in one paragraph).

## 22. Intensifiers

Why: empty emphasis words pile up in place of an actual reason.

- really, truly, genuinely, deeply, incredibly, literally, actually, basically, simply, just, absolutely, definitely
- RU: «буквально», «реально», «действительно», «по-настоящему», «абсолютно», «просто», «очень», «максимально», «крайне»

Fix: keep the ones that change the meaning ("actually" correcting a fact, «просто» meaning "easy").

Linter: `intensifiers.en` S, `intensifiers.ru` S (three or more in one paragraph).

## 23. Generic praise

- amazing, incredible, fantastic, outstanding, stunning, phenomenal; RU: «потрясающий», «невероятный», «восхитительный», «великолепный», «шикарный»

Fix: the detail that earned the praise ("the bread was still warm at noon").

Linter: `praise.en` S, `praise.ru` S (two or more in one paragraph).

## 24. Absolutes

- everyone, nobody, always, never, everything, every single; RU: «всегда», «никогда», «никто», «каждый», «абсолютно все»

Fix: narrow to what you know ("most of the people I asked").

Linter: `absolutes.en` S, `absolutes.ru` S (three or more in one paragraph).

## 25. Emphasis crutches, permission slips, fake intimacy

- crutches: "Let that sink in.", "Read that again.", "Make no mistake.", "This changes everything.", "Full stop.", "Period."; RU: «Вдумайтесь.», «Перечитайте ещё раз.», «Это меняет всё.», «Точка.»
- permission slips: "And that's okay.", «И это нормально.»
- intimacy: "Trust me", "Between you and me", "Real talk", "I promise", "You're not alone", "You've got this"; RU: «Поверьте», «Между нами», «Открою секрет», «Буду с вами откровенен», «Вы не одни»

Fix: cut. If the point needs a crutch, sharpen the point.

Linter: `crutch.en` H, `crutch.en-stop` H, `crutch.ru` H, `crutch.ru-stop` H, `intimacy.en` S, `intimacy.ru` S.

## 26. Recaps and morals

Why: the text summarizes what the reader has just read, or ends each paragraph with its lesson.

- "In conclusion", "To sum up", "In summary"; SOFT: "Ultimately", "All in all", "The bottom line", "Key takeaways", "Long story short"
- RU: «В заключение», «Подводя итог», «Подытожим», «Резюмируя»; SOFT: «В итоге», «Таким образом», «В сухом остатке», «Одним словом»
- morals: "By doing X, you'll Y.", "That's what it's all about.", «Делая X, вы получаете Y.»

Fix: end on the last new point or on the ask.

Linter: `recap.marker` H, `recap.ru-marker` H, `recap.soft` S, `recap.ru-soft` S, `recap.paragraph` S (a closing paragraph whose words mostly appeared above, with no new numbers).

## 27. False agency, hidden actors, narrator at a distance

Why: abstractions do people's work, so nobody is responsible and nothing is concrete.

- "The data tells us…", "The market rewards…", "The decision emerged", "Mistakes were made", "People tend to…", "Nobody designed this."
- RU: «Данные говорят…», «Рынок решает…», «Решение пришло само», «Было решено…», «Считается, что…», «Люди склонны…»

Fix: name who did it ("We read the March numbers and decided…"); "you" beats "people". Keep the passive where the actor is unknown or irrelevant.

Linter: `agency.en` S, `agency.ru` S, `passive.en` S (three or more passives in one paragraph).

## 28. One thing, many names

Why: a repetition penalty makes the text rotate names: the app, the platform, the tool, the solution. The reader wonders whether these are four things.

Fix: pick one name and repeat it.

Linter: `names.cycling` S (four or more of app/platform/tool/solution/service/product/system, or the Russian equivalents).

## 29. "X by X"

Why: "step by step", "week by week", «шаг за шагом» promise a walkthrough that usually never comes.

Fix: keep it only if the text really goes through each step; otherwise name what the reader gets ("what changes in each of the three phases").

Not a hit: one by one, side by side, little by little.

Linter: `xbyx.en` S, `xbyx.ru` S.

## 30. Rhythm

- metronome: four or more sentences of nearly one length
- punchlines: most paragraphs end on a short sentence after a long one
- feed layout: one sentence per paragraph, all the way down
- a question asked and answered at once

Fix: let length follow content; let some paragraphs end on a plain fact; group sentences that belong together.

Linter: `rhythm.metronome` S, `rhythm.punchlines` S, `rhythm.one-line-paragraphs` S, `fragment.*`.

## 31. Lists and layout

- lists where every item is "**Label:** explanation", items of identical length, every item starting with the same word, emoji as bullets
- headings in a 200-word post, bold every other sentence, emoji confetti, five hashtags, three exclamation marks

Fix: prose by default; keep the formatting the venue really needs.

Linter: `list.bold-labels` S, `list.uniform` S, `list.same-start` S, `list.emoji-bullets` S, `layout.headings` S, `layout.bold` S, `layout.emoji` S, `layout.hashtags` S, `layout.exclamations` S.

## 32. First-person replies

For forum posts, comments, replies and DMs written as a person:

- answer the one or two points that matter; don't give each point of the other person its own paragraph
- vary sentence openings; a run of "The [noun]…" sentences reads like an essay
- keep hyphenated compound modifiers to standard ones (follow-up, off-label, day-to-day); spell the rest out
- the author's casual habits (lowercase, short lines, local slang) are voice, not errors

Linter: `rhythm.the-openers` S, `rhythm.hyphen-compounds` S.

## 33. Sameness across a batch

Several texts published together share a template even when each is fine alone: every one ends with a question, opens with the same move, or has the same length and shape.

Fix: vary the first move and the ending; at most a third end on a question.

Linter: `batch.questions` S, `batch.openers` S (with `--batch`).

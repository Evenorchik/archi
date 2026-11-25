#!/usr/bin/env python3
"""clean_copy_check.py: the countable first pass of the clean-copy skill.

Scans Russian and English text that is about to be published or sent for
traces of machine-written prose. HARD hits must all be fixed. SOFT hits are
for review: one of them is usually fine, a cluster usually is not.

A clean run means "nothing countable was found". It never means the text is
good; the checklist in references/checklist.md is the actual review.

Usage
  clean_copy_check.py DRAFT [--lang auto|en|ru|uk] [--ad] [--batch] [--json]
  clean_copy_check.py --text "draft text" [options]
  clean_copy_check.py - [options]              (read standard input)
  clean_copy_check.py --list-rules

Options
  --lang      auto (default) decides from the letters in the text. There are
              no Ukrainian word lists: for uk only the language-neutral checks
              run (dashes, quotes, fragments, rhythm, layout).
  --ad        advertising copy. Audience call-outs, engagement prompts and
              slogan fragments drop to SOFT; claim and urgency checks are added.
  --batch     several texts separated by a line of ---, === or ***. Each text
              is checked on its own, then the set is checked for sameness.
  --quotes    also check blockquote lines (starting with >). Skipped by
              default: quoted words belong to someone else.
  --hard-only print HARD hits only.
  --json      print JSON.

Exit status: 0 no HARD hits, 1 at least one HARD hit, 2 usage or input error.
Standard library only.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass, field

HARD, SOFT, OFF = "HARD", "SOFT", "OFF"
FLAGS = re.IGNORECASE | re.MULTILINE

AP = "['’]"  # apostrophe, straight or curly
# Start of a sentence, for rules that scan the whole text.
SS = (r"(?:^|(?<=[.!?…]\s)|(?<=[.!?…][\"»”’)]\s)|(?<=\x00))"
      r"(?:[-*+•]\s+|\*\*)?")


# --------------------------------------------------------------------------
# Rule model
# --------------------------------------------------------------------------
# where:
#   anywhere  search the whole text
#   sentence  must match at the start of a sentence
#   opening   start of one of the first two sentences of a text
#   closing   start of one of the last three sentences of a text
# ad: severity in --ad mode (SOFT or OFF); None keeps the normal severity.


@dataclass
class Rule:
    rid: str
    sev: str
    lang: str
    where: str
    pattern: str
    reason: str
    fix: str
    ad: str | None = None
    ad_only: bool = False
    rx: re.Pattern = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.rx = re.compile(self.pattern, FLAGS)


@dataclass
class Cluster:
    """Words that are fine alone and a tell in a bunch (counted per paragraph)."""
    rid: str
    lang: str
    pattern: str
    threshold: int
    reason: str
    fix: str
    rx: re.Pattern = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.rx = re.compile(self.pattern, FLAGS)


RULES: list[Rule] = []
CLUSTERS: list[Cluster] = []


def rule(rid, sev, lang, where, pattern, reason, fix, ad=None, ad_only=False):
    RULES.append(Rule(rid, sev, lang, where, pattern, reason, fix, ad, ad_only))


def cluster(rid, lang, pattern, threshold, reason, fix):
    CLUSTERS.append(Cluster(rid, lang, pattern, threshold, reason, fix))


# ---- openers: throat-clearing, drumrolls, stock scene-setting -------------
rule("opener.drumroll", HARD, "en", "sentence",
     r"here" + AP + r"?s (?:the (?:thing|deal|kicker|catch|truth|reality|secret|problem|twist)|what|why)\b",
     "drumroll opener: promises a point instead of making it",
     "delete it and open with the point")
rule("opener.preview", SOFT, "en", "sentence", r"here" + AP + r"?s how\b",
     "preview opener", "fine before real instructions; otherwise start with step one")
rule("opener.truth", HARD, "en", "sentence",
     r"(?:the (?:simple |hard |uncomfortable |honest |brutal |plain |real |sad )?truth is|the reality is"
     r"|truth be told|let me be (?:clear|honest|frank|blunt)|let" + AP + r"s (?:be (?:honest|real|clear|frank)|face it))\b",
     "throat-clearing opener", "state the claim itself")
rule("opener.sincerity", SOFT, "en", "sentence",
     r"(?:honestly|frankly|to be (?:honest|fair|frank)|i" + AP + r"ll be honest|i have to (?:say|admit)"
     r"|i must (?:say|admit)|it turns out|to my (?:surprise|astonishment))\b",
     "sincerity or surprise marker as an opener", "drop it unless it introduces a real correction")
rule("opener.spoken", SOFT, "en", "sentence", r"(?:so|look|listen|okay so|well),\s",
     "spoken preamble", "start with the content")
rule("opener.hook", HARD, "en", "anywhere",
     r"\bit" + AP + r"s no secret that\b|\bwe" + AP + r"ve all been there\b|\bpicture this\b|\bwhat if i told you\b",
     "stock hook", "open with the fact or the scene itself")
rule("opener.wondered", HARD, "en", "sentence",
     r"(?:have you )?ever wondered\b|imagine (?:a world|this|for a moment|if you could)\b",
     "rhetorical hook", "say the thing you want them to picture", ad=SOFT)
rule("opener.era", HARD, "en", "anywhere",
     r"\bin (?:today" + AP + r"s|this|our) (?:(?:fast-paced|digital|modern|ever-changing|rapidly (?:evolving|changing)"
     r"|hyper-connected|always-on|competitive|busy)\s+){1,2}(?:world|age|era|landscape|environment|market|economy)\b"
     r"|\bin (?:a|an) (?:world|era|age) (?:where|of|when)\b"
     r"|\bin the (?:ever-evolving|ever-changing|fast-paced|rapidly evolving) (?:world|landscape|field|realm) of\b",
     "school-essay scene-setting", "start with what happened or what you offer")
rule("opener.ru-sincerity", HARD, "ru", "sentence",
     r"(?:если честно|честно говоря|по правде говоря|откровенно говоря|скажу (?:честно|прямо|откровенно)"
     r"|буду (?:честен|честна|откровенен|откровенна))\s*[,:]",
     "sincerity marker as an opener", "start with the claim")
rule("opener.ru-throat", HARD, "ru", "sentence",
     r"(?:вот в ч[её]м (?:дело|штука|фишка)|не буду ходить вокруг да около|давайте (?:разбер[её]мся|начистоту"
     r"|будем честны|посмотрим правде в глаза|поговорим (?:о|об|про))|обо вс[её]м по порядку"
     r"|разбер[её]мся по порядку|начн[её]м с главного)\b",
     "throat-clearing opener", "start with the point")
rule("opener.ru-spoken", SOFT, "ru", "sentence",
     r"(?:на самом деле|дело в том,? что|знаете(?:,? что)?|слушайте|итак|сразу скажу|начну с того,? что"
     r"|начн[её]м с того,? что|представьте(?: себе)?)[,:]?\s",
     "spoken preamble", "keep only if it introduces a real correction")
rule("opener.ru-hook", HARD, "ru", "anywhere",
     r"\bни для кого не секрет\b|\bне секрет,? что\b|\bмы все через это проходили\b"
     r"|\bа что,? если я (?:скажу|вам скажу)\b",
     "stock hook", "open with the fact")
rule("opener.ru-wondered", HARD, "ru", "sentence",
     r"(?:вы когда-нибудь задумывались|задумывались ли вы|вы когда-нибудь замечали)\b",
     "rhetorical hook", "say it", ad=SOFT)
rule("opener.ru-era", HARD, "ru", "anywhere",
     r"\bв современном мире\b|\bв (?:нашу |современную )?(?:цифровую|информационную) эпоху\b"
     r"|\bв эпоху (?:цифров|информацион|технолог|искусствен|перемен|соцсет|быстр)\w*|\bв мире,? где\b"
     r"|\bв условиях (?:современн|постоянно|стремительно|быстро)\w*",
     "school-essay scene-setting", "start with what happened or what you offer")

# ---- signposting and meta-commentary -------------------------------------
rule("signpost.announce", HARD, "en", "sentence",
     r"(?:let me (?:walk|take) you through|let" + AP + r"s (?:dive|dig|jump) (?:in|into|right in)"
     r"|let" + AP + r"s (?:break (?:it|this|that) down|unpack|take a (?:closer|deeper) look|explore)"
     r"|buckle up|first things first|before we (?:dive|begin|get started)"
     r"|in this (?:post|article|thread|guide|piece|email|newsletter|video)[, ]+(?:i|we)"
     r"(?:" + AP + r"ll| will| want to| are going to|" + AP + r"re going to))\b",
     "announces the structure instead of delivering it", "cut the announcement; the next sentence is the start")
rule("signpost.ado", HARD, "en", "anywhere", r"\bwithout further ado\b",
     "announcement filler", "cut it")
rule("signpost.aside", SOFT, "en", "anywhere",
     r"\b(?:spoiler(?: alert)?|plot twist|fun fact|pro tip|hint|hot take|unpopular opinion|quick note|side note)\s*:"
     r"|\bmore on (?:that|this) (?:below|later)\b|\bas (?:we" + AP + r"ll|you" + AP + r"ll|i" + AP + r"ll) see\b"
     r"|\bthat" + AP + r"s (?:a (?:topic|story|post) for another|another story for another) (?:day|post|time)\b"
     r"|\bbut i digress\b",
     "meta-commentary label", "drop the label and say the thing")
rule("signpost.ru-announce", HARD, "ru", "sentence",
     r"(?:в (?:этой|данной|сегодняшней) (?:статье|заметке|публикации|посте|рассылке|подборке|инструкции)[, ]+"
     r"(?:я |мы )?(?:расскаж|рассмотр|разбер|поговор|покаж|объясн|узна)\w*"
     r"|ниже (?:я |мы )?(?:расскаж|рассмотр|разбер|покаж|объясн)\w*"
     r"|сейчас (?:объясню|расскажу|покажу|разбер[её]мся)|разложу (?:вс[её] )?по полочкам"
     r"|давайте (?:рассмотрим|разбер[её]м|посмотрим|взглянем|разложим))",
     "announces the structure instead of delivering it", "cut the announcement")
rule("signpost.ru-aside", SOFT, "ru", "anywhere",
     r"\b(?:спойлер|лайфхак|важный момент|небольшой дисклеймер|забегая вперёд|забегая вперед)\s*:"
     r"|\bоб этом (?:ниже|чуть ниже|позже|дальше|в следующем посте)\b"
     r"|\bно это уже (?:другая история|тема для отдельного поста|совсем другая история)\b"
     r"|\bкак (?:мы|вы) (?:увидим|скоро увидите|увидите дальше)\b|\bпоехали[!.]",
     "meta-commentary", "drop it and say the thing")

# ---- binary contrasts: "not X, but Y" ------------------------------------
_IT = r"(?:it|this|that)"
_NEG = r"(?:" + AP + r"s not|\s+is not|\s+isn" + AP + r"t|\s+was not|\s+wasn" + AP + r"t)"
_POS = r"(?:" + AP + r"s|\s+is|\s+was)"
rule("contrast.its-not", HARD, "en", "anywhere",
     r"\b" + _IT + _NEG + r"\s+(?:just |only |merely |about |really )?[^.!?\n]{1,50}?(?:[.;,:]|\s[—–-])\s*"
     + _IT + _POS + r"\b",
     "staged reversal 'it's not X, it's Y'", "say Y, and why, without the decoy")
rule("contrast.isnt-just", HARD, "en", "anywhere",
     r"\b(?:is|are|was|were)n" + AP + r"t (?:just|only|merely|simply) [^.!?\n]{1,40}?(?:[,;.:]|\s[—–-])\s*"
     r"(?:it|they|this|that)(?:" + AP + r"s|" + AP + r"re| is| are| was| were)\b",
     "upsell reversal 'isn't just X, it's Y'", "say what it is")
rule("contrast.problem-isnt", HARD, "en", "anywhere",
     r"\bthe (?:real )?(?:problem|question|answer|point|issue|goal|secret|lesson|bottleneck) "
     r"(?:isn" + AP + r"t|is not|wasn" + AP + r"t|was not) [^.!?\n]{1,50}?[.;,]\s+(?:it" + AP + r"s|it is|it was"
     r"|the (?:real )?(?:problem|question|answer|point|issue) is)\b"
     r"|\b\w+(?: \w+){0,3} (?:isn" + AP + r"t|is not|aren" + AP + r"t|are not) the (?:problem|issue|enemy|bottleneck)"
     r"[.;,]\s+\w+(?: \w+){0,3} (?:is|are)\b",
     "formula reframe 'the problem isn't X, it's Y'", "name the problem")
rule("contrast.not-because", HARD, "en", "anywhere",
     SS + r"not because [^.!?\n]{1,60}?[.!]\s+(?:but )?because\b",
     "telegraphed reversal 'Not because X. Because Y.'", "give the real reason once")
rule("contrast.not-just", SOFT, "en", "anywhere",
     r"\bnot (?:just|only|merely|simply) (?:a |an |the )?[^.!?\n]{1,40}?,?\s+but (?:also )?"
     r"|\bnot because [^.!?\n]{1,60}?,?\s+but because\b"
     r"|\b(?:it" + AP + r"s|it is|this is) less about [^.!?\n]{1,50}? and more about\b"
     r"|\bstops? being [^.!?\n]{1,40}? and starts? being\b|\ba feature,? not a bug\b",
     "contrast scaffolding", "say the positive claim directly")
rule("contrast.ru-eto-ne", HARD, "ru", "anywhere",
     r"\bэто не (?:просто |только |про |о |об |совсем )?[^.!?\n]{1,50}?(?:[.;,:]|\s[—–-])\s*это\b",
     "staged reversal 'это не X, это Y'", "say Y directly")
rule("contrast.ru-ne-prosto", HARD, "ru", "anywhere",
     r"\bне просто [^.!?\n]{1,50}?(?:,|\s[—–-])\s*(?:а|это)\b",
     "upsell reversal 'не просто X, а Y'", "say what it is")
rule("contrast.ru-delo", HARD, "ru", "anywhere",
     r"\b(?:дело|проблема|вопрос|секрет|суть|задача|цель) не в [^.!?\n]{1,50}?(?:[,.;]|\s[—–-])\s*"
     r"(?:а |(?:дело|проблема|вопрос|секрет|суть|задача|цель) )в\b"
     r"|\bречь не (?:о|об|про) [^.!?\n]{1,50}?(?:[,.;]|\s[—–-])\s*(?:а|речь)\b"
     r"|\bне про [^.!?\n]{1,40}?(?:[,.;]|\s[—–-])\s*а про\b",
     "formula reframe 'дело не в X, а в Y'", "name the cause")
rule("contrast.ru-soft", SOFT, "ru", "anywhere",
     r"\bне потому,? что [^.!?\n]{1,60}?,\s*а потому,? что\b|\bэто не баг,? а фич\w*",
     "reversal by negation", "give the reason directly")
rule("contrast.ru-ne-a", SOFT, "ru", "sentence", r"не [^.!?\n,]{1,40}, а ",
     "sentence opens with 'Не X, а Y'", "fine for a factual correction; a tell as a rhetorical move")

# ---- template closers, engagement bait, assistant frame -------------------
rule("closer.curious", HARD, "en", "anywhere",
     r"\bi" + AP + r"?d love to (?:hear|know|see|learn) (?:your|what|how|from|about|if|whether)\b"
     r"|\bi" + AP + r"?m (?:really |genuinely |so )?curious (?:to (?:hear|know|see|learn)|what|how|whether|if|about)\b"
     r"|\b(?:i" + AP + r"?d be|i am|i" + AP + r"?m) (?:curious|interested) to hear\b"
     r"|\bcurious to (?:hear|know) (?:what|how|your)\b|\bi wonder if (?:anyone|others|other people|you)\b",
     "template engagement close", "end on your last real point, or ask one plain question", ad=SOFT)
rule("closer.bait", HARD, "en", "anywhere",
     r"\blet me know (?:your thoughts|what you think|in the comments|below|if you agree)\b"
     r"|\b(?:drop|leave|share|post) (?:a comment|your thoughts|your take|yours) (?:below|in the comments)\b"
     r"|\bshare your (?:thoughts|experiences?|take|stories)\b|\bwhat" + AP + r"s your take\b"
     r"|\b(?:follow|subscribe) for more\b|\btag (?:someone|a friend|anyone) who\b|\brepost if\b"
     r"|\bsmash (?:that|the) (?:like|subscribe)\b|\bif you found this (?:helpful|useful|valuable)\b"
     r"|\bam i the only one\b",
     "engagement bait", "cut it; if you need replies, ask one specific question", ad=SOFT)
rule("closer.tag-question", HARD, "en", "closing",
     r"(?:thoughts|agree|agreed|anyone else|right|what do you (?:all |guys )?think|what about you|how about you"
     r"|who else)\s*\?",
     "stock closing question", "close on a statement, or ask what only these readers can answer", ad=SOFT)
rule("closer.anyone-else", SOFT, "en", "anywhere", r"\b(?:has|does|did) anyone else\b",
     "common forum question; a tell when every post ends with it", "keep it if it is the real question")
rule("assistant.frame", HARD, "en", "anywhere",
     r"\b(?:i )?hope (?:this|that|it) (?:helps|was helpful|clarifies)\b|\bhope this (?:e-?mail|message|note|letter) finds you\b"
     r"|\bas an ai\b|\bas a (?:large )?language model\b",
     "chat-assistant or form-letter frame", "delete it; the text is not a reply to a user")
rule("assistant.praise", HARD, "en", "sentence",
     r"(?:what an? |that" + AP + r"s an? |such an? )?(?:great|excellent|fantastic|wonderful|brilliant|amazing|insightful)"
     r" (?:question|point|idea|post|catch)\b|you" + AP + r"re (?:absolutely|completely|totally|so) right\b",
     "sycophantic opener", "answer the question")
rule("assistant.yes", HARD, "en", "opening", r"(?:certainly|absolutely|of course|sure thing)!",
     "eager-assistant opener", "start with the answer")
rule("assistant.formula", SOFT, "en", "anywhere",
     r"\bfeel free to\b|\b(?:please )?(?:don" + AP + r"t|do not) hesitate to\b"
     r"|\blet me know if you have any (?:other |further |more )?questions\b|\bi" + AP + r"?d be (?:happy|glad|delighted) to\b"
     r"|\bhappy to help\b|\bthank you for reaching out\b|\bi hope you" + AP + r"?re (?:doing )?well\b"
     r"|\bi wanted to reach out\b|\bjust (?:wanted to )?(?:follow(?:ing)? up|check(?:ing)? in|circl(?:e|ing) back)\b"
     r"|\bthanks for sharing\b|\bcouldn" + AP + r"t agree more\b|\blove this\b|\bgood question\b|\bgreat point\b",
     "form-letter or chat-assistant formula", "keep only if this author would say it")
rule("closer.ru-bait", HARD, "ru", "anywhere",
     r"\b(?:пишите|напишите|расскажите|делитесь|поделитесь) (?:в комментари\w*|сво\w+ (?:мнени\w*|опыт\w*|истори\w*)"
     r"|опытом|мнением|ниже)"
     r"|\bжду (?:ваши\w* )?(?:комментари\w*|мнени\w*|истори\w*|отзыв\w*) (?:в комментари\w*|ниже)"
     r"|\b(?:ставьте|поставьте|жмите) (?:лайк|плюс|огонь|сердечко)\w*|\bподписывайтесь\b"
     r"|\bсохрани(?:те)? (?:себе|пост|чтобы не потерять)\b|\bсохраняйте,? чтобы не потерять\b"
     r"|\bперешлите (?:тому|друг\w*|тем|коллег\w*)\b",
     "engagement bait", "cut it; if you want answers, ask one concrete question", ad=SOFT)
rule("closer.ru-curious", HARD, "ru", "anywhere",
     r"\b(?:мне |было бы )?(?:интересно|любопытно) (?:узнать|услышать|почитать) (?:ваше|ваши|как|что|мнени\w*)"
     r"|\bбуду (?:рад|рада|рады|благодарен|благодарна) (?:услышать|узнать|прочитать|вашим|любым)\b"
     r"|\bинтересно ваше мнение\b",
     "template engagement close", "end on your last point", ad=SOFT)
rule("closer.ru-wonder", HARD, "ru", "closing",
     r"(?:мне )?(?:очень )?интересно,? (?:как|что|какой|какие|кто|почему|а как) (?:у вас|вы|другие|у других)\b",
     "template engagement close", "end on your last point", ad=SOFT)
rule("closer.ru-tag-question", HARD, "ru", "closing",
     r"(?:а (?:как|что) (?:у вас|думаете вы|вы думаете|скажете вы)|а вы как|что (?:думаете|скажете)"
     r"|как (?:считаете|думаете)|согласны|кто со мной|узнали себя)\s*\?",
     "stock closing question", "close on a statement, or ask one concrete question", ad=SOFT)
rule("closer.ru-anyone", SOFT, "ru", "anywhere",
     r"\bкто-(?:нибудь|то) (?:ещ[её] )?(?:сталкивал|пробовал|замечал)\w*",
     "common forum question; a tell when every post ends with it", "keep it if it is the real question")
rule("assistant.ru-frame", HARD, "ru", "anywhere",
     r"\bнадеюсь,? (?:это|эта информация|мой ответ|я|статья|материал|было) (?:поможет|помогла|помог|полезн\w*"
     r"|была полезн\w*|пригодится|ответил\w*)"
     r"|\bнадеюсь,? (?:это |мо[её] )?письмо застанет\b|\bкак (?:ии|языковая модель|искусственный интеллект)[, ]",
     "chat-assistant or form-letter frame", "delete it")
rule("assistant.ru-praise", HARD, "ru", "sentence",
     r"(?:отличный|прекрасный|замечательный|великолепный|потрясающий) вопрос\b|вы (?:абсолютно|совершенно|полностью) правы\b",
     "sycophantic opener", "answer the question")
rule("assistant.ru-formula", SOFT, "ru", "anywhere",
     r"\bхороший вопрос\b|\bсовершенно верно\b|\bобращайтесь,? если\b"
     r"|\bне стесняйтесь (?:обращаться|писать|спрашивать|задавать)\b|\bчувствуйте себя свободно\b"
     r"|\bесли (?:у вас )?(?:остались|возникнут|появятся|есть) (?:какие-либо |какие-то |ещ[её] )?вопросы\b"
     r"|\bбуду (?:рад|рада|рады) помочь\b|\bнадеюсь,? у (?:вас|тебя) вс[её] (?:хорошо|отлично)\b"
     r"|\bспасибо,? что поделил\w*|\bблагодарю за обращение\b|\bс радостью (?:помогу|отвечу)\b",
     "form-letter or chat-assistant formula", "keep only if this author would say it")

# ---- audience call-outs ----------------------------------------------------
rule("audience.callout", HARD, "en", "sentence",
     r"(?:for (?:anyone|those|people|everyone|all of you|folks) (?:who|that)"
     r"|if you" + AP + r"re (?:someone|one of those people|the kind of person|the type of person) who"
     r"|this (?:one|post|thread|guide) is for (?:anyone|everyone|those|you if))\b",
     "audience call-out opener (reads like ad targeting)",
     "fold the audience into the sentence: ask them directly or describe what happened to them", ad=SOFT)
rule("audience.whether", HARD, "en", "anywhere",
     r"\bwhether you" + AP + r"re (?:a |an |just )?[^.!?\n,]{1,40}? or (?:a |an )?[^.!?\n,]{1,40}?,",
     "'whether you're X or Y' sweep", "pick the reader you mean", ad=SOFT)
rule("audience.soft", SOFT, "en", "anywhere",
     r"\bif you" + AP + r"ve ever (?:wondered|struggled|felt|tried|wanted|found yourself)\b"
     r"|\bfrom (?:complete |total |absolute )?(?:beginners?|novices?|newbies|startups?|small businesses|students|first-timers)"
     r" to (?:seasoned |experienced )?(?:experts?|pros?|professionals?|enterprises?|veterans?|large (?:companies|enterprises))\b",
     "audience sweep", "name who it is actually for")
rule("audience.ru-callout", HARD, "ru", "sentence",
     r"(?:для (?:тех|всех),? кто|если (?:вы|ты) из тех,? кто"
     r"|этот (?:пост|текст|материал|гайд) (?:для|[—–-] для) (?:тех|всех|вас))\b",
     "audience call-out opener (reads like ad targeting)", "fold the audience into the sentence", ad=SOFT)
rule("audience.ru-whether", HARD, "ru", "anywhere",
     r"\b(?:неважно|не важно|без разницы),? [^.!?\n,]{1,40}? (?:вы|ты) или [^.!?\n,]{1,40}?,"
     r"|\b(?:неважно|не важно),? (?:вы|ты) [^.!?\n,]{1,40}? или\b|\bбудь (?:вы|ты) [^.!?\n,]{1,40}? или\b",
     "'неважно, новичок вы или профи' sweep", "pick the reader you mean", ad=SOFT)
rule("audience.ru-soft", SOFT, "ru", "anywhere",
     r"\bесли (?:вы|ты) когда-(?:нибудь|либо) (?:задумывал|сталкивал|пробовал|мечтал|хотел)\w*"
     r"|\bот (?:новичк|начинающ|студент|стартап|малого бизнеса|школьник)\w* до (?:профи|профессионал|эксперт|опытн"
     r"|крупн|корпорац|пенсионер)\w*|\bбудь то [^.!?\n,]{1,40}? или\b",
     "audience sweep", "name who it is actually for")

# ---- outsider disclosure as the first line --------------------------------
rule("outsider.open", HARD, "en", "opening",
     r"(?:(?:full |quick )?(?:disclosure|disclaimer):\s*)?"
     r"(?:i" + AP + r"?m (?:not (?:a|an|really a|much of a)|no) [\w-]+(?: [\w-]+)?,? but\b"
     r"|i" + AP + r"?m not (?:an expert|a professional|a specialist|an insider)\b"
     r"|as (?:an outsider|someone (?:outside|who isn" + AP + r"t|who" + AP + r"s not|with no|from outside))\b"
     r"|i don" + AP + r"t usually post (?:here|on|about)\b"
     r"|i know (?:nothing|very little|next to nothing) about [^.!?\n]{1,40}?,? but\b)",
     "opens with an outsider disclosure: readers leave before the point",
     "open with the observation or question; put your background in paragraph two or three")
rule("outsider.ru-open", HARD, "ru", "opening",
     r"(?:(?:сразу )?(?:оговорюсь|дисклеймер)\s*[:,]?\s*)?"
     r"(?:я (?:вообще |сам |сама )?не (?:врач|юрист|психолог|эксперт|специалист|профессионал|программист|разработчик"
     r"|дизайнер|маркетолог|фотограф|инженер|медик|экономист|финансист|учитель|педагог|блогер|писатель|журналист"
     r"|айтишник|из (?:этой|вашей) (?:сферы|тусовки|области|темы|отрасли))\b"
     r"|как человек со стороны"
     r"|(?:я )?(?:тут|здесь) (?:человек )?(?:новый|новенький|новенькая),? но"
     r"|не претендую на (?:истину|экспертность|экспертизу)|я никогда не [\w-]+(?: [\w-]+)?,? но\b)",
     "opens with an outsider disclosure: readers leave before the point",
     "open with the observation; put your background in paragraph two or three")
rule("outsider.ru-maybe", SOFT, "ru", "opening", r"я не [\w-]+,? но\b",
     "possible outsider disclosure in the first sentence", "if it is one, move it lower")

# ---- academic vocabulary in casual text ------------------------------------
rule("academic.posture", HARD, "any", "anywhere",
     r"\bn\s?=\s?1\b|\bsample (?:size )?of (?:one|1)\b|\banecdata\b|\bвыборк\w* (?:из )?(?:одного|1)\b",
     "analyst posture in a personal text", "say 'that is just how it went for me' / 'проверял только на себе', or drop it")
rule("academic.terms", SOFT, "en", "anywhere",
     r"\bstatistically significant\b|\bconfirmation bias\b|\bcontrol group\b|\bp-value\b"
     r"|\bcorrelation (?:is not|isn" + AP + r"t|does not equal|doesn" + AP + r"t (?:equal|mean)) causation\b"
     r"|\bmethodology\b|\bself-reported\b|\bdata points?\b|\bempirical(?:ly)?\b|\bparadigm\b|\bfirst principles\b"
     r"|\bsurvivorship bias\b|\bcognitive (?:load|dissonance|bias(?:es)?)\b",
     "research vocabulary: right in a study, posture in a peer post",
     "use everyday words: 'I only tried it on myself', 'could be a coincidence'")
rule("academic.ru-terms", SOFT, "ru", "anywhere",
     r"\bстатистически значим\w*|\bконтрольн\w* групп\w*|\bошибк\w* выжившего\b|\bпредвзятост\w* подтверждения\b"
     r"|\bкогнитивн\w* (?:искажени|диссонанс|нагрузк)\w*|\bметодологи\w*|\bэмпирическ\w*|\bпарадигм\w*"
     r"|\bдихотоми\w*|\bкорреляци\w*|\bанекдотическ\w* (?:данн|свидетельств)\w*|\bрепрезентативн\w*",
     "research vocabulary: right in a study, posture in a peer post", "use everyday words")

# ---- inflated vocabulary, filler, copula dodges ----------------------------
rule("inflated.core", HARD, "en", "anywhere",
     r"\bdelv(?:e|es|ed|ing)\b|\btapestr(?:y|ies)\b|\ba testament to\b|\bbeacon of\b|\bembark(?:s|ed|ing)? on\b"
     r"|\bever-evolving\b|\bin the realm of\b"
     r"|\bunlock(?:s|ed|ing)? (?:the |your |its |their |our |a |new )?(?:full |true |hidden |real )?"
     r"(?:potential|power|possibilities|secrets?)\b"
     r"|\bharness(?:es|ed|ing)? the (?:power|potential)\b|\bunleash(?:es|ed|ing)?\b|\bgame[- ]?chang(?:er|ers|ing)\b"
     r"|\brevolutioni[sz](?:e|es|ed|ing) the way\b"
     r"|\bnavigat(?:e|es|ing) the (?:complexities|complex|challenges|landscape|intricacies|waters)\b"
     r"|\bparadigm shift\b|\b(?:rich|vibrant|intricate) (?:tapestry|mosaic|interplay)\b|\bintricate dance\b"
     r"|\ba symphony of\b|\b(?:take|takes|taking) (?:it|things|your [\w ]{1,25}?) to the next level\b"
     r"|\belevat(?:e|es|ing) (?:your|our|their) \w+|\bsupercharg(?:e|es|ed|ing)\b",
     "stock machine vocabulary or hype verb", "say what actually happens, in plain words")
rule("inflated.soft", SOFT, "en", "anywhere",
     r"\bseamless(?:ly)?\b|\brobust\b|\bcutting[- ]edge\b|\bstate[- ]of[- ]the[- ]art\b|\bworld[- ]class\b"
     r"|\bbest[- ]in[- ]class\b|\bholistic\b|\bmultifaceted\b|\bpivotal\b|\bparamount\b|\bmyriad\b|\bplethora\b"
     r"|\bmeticulous(?:ly)?\b|\bunderscor(?:e|es|ed|ing)\b|\bshowcas(?:e|es|ed|ing)\b|\bfoster(?:s|ed|ing)?\b"
     r"|\bleverag(?:e|es|ed|ing)\b|\bempower(?:s|ed|ing|ment)?\b|\btransformative\b|\bgroundbreaking\b"
     r"|\bunparalleled\b|\brevolutioni[sz](?:e|es|ed|ing)\b|\b(?:thrilled|excited|delighted|proud) to (?:announce|share|introduce|unveil)\b"
     r"|\bjourney\b|\blandscape\b|\bresonat(?:e|es|ed|ing)\b|\bnestled\b|\bbustling\b|\bvibrant\b|\binvaluable\b"
     r"|\bnuanced\b|\bcrucial\b",
     "inflated word: one is fine, several make a brochure", "use the plain word or the concrete detail")
rule("filler.importance", HARD, "en", "anywhere",
     r"\bit" + AP + r"s (?:important|crucial|essential|vital) to (?:note|remember|understand|recognize|keep in mind)\b",
     "announces importance instead of showing it", "state the thing; the reader will see why it matters")
rule("filler.phrase", SOFT, "en", "anywhere",
     r"\bit" + AP + r"s worth (?:noting|mentioning|pointing out)\b|\bat its core\b|\bwhen it comes to\b"
     r"|\bat the end of the day\b|\bneedless to say\b|\bthe fact of the matter is\b"
     r"|\bplays? an? (?:key|crucial|vital|pivotal|important|significant) role\b|\bfirst and foremost\b"
     r"|\blast but not least\b|\bas (?:we all|everyone|you all|you probably) know\b",
     "filler phrase", "cut it or say the specific thing")
rule("copula.dodge", SOFT, "en", "anywhere",
     r"\bserves as (?:a|an|the)\b|\bacts as (?:a|an) (?:bridge|catalyst|hub|gateway|cornerstone)\b|\bstands as\b"
     r"|\bboasts (?:a|an|over|more than|\d)|\brepresents (?:a|an) (?:significant|major|new|real|key) (?:step|milestone|shift|leap)\b",
     "dodges a plain 'is' or 'has'", "use 'is' or 'has'")
rule("inflated.ru-core", HARD, "ru", "anywhere",
     r"\bпогру(?:зиться|зитесь|жаемся|зимся|жайтесь|зись) в (?:мир|атмосферу|удивительн\w*|увлекательн\w*|волшебн\w*)"
     r"|\bокун(?:уться|итесь|[её]мся|ись) в (?:мир|атмосферу)\b|\bраскр\w*\s+(?:\w+\s+){0,2}потенциал\w*"
     r"|\bна (?:совершенно |качественно |принципиально )?новый уровень\b|\bоткройте для себя\b"
     r"|\b(?:трудно|сложно|невозможно) переоценить\b",
     "stock vocabulary of generated Russian text", "say plainly what happens or what you offer")
rule("inflated.ru-soft", SOFT, "ru", "anywhere",
     r"\bнеотъемлем\w* част\w*|\bигра(?:ет|ют|л|ла|ли|ть) (?:ключевую|важнейшую|решающую|огромную|немаловажную|важную"
     r"|значительную) роль\b|\bуникальн\w*|\bинновационн\w*|\bреволюционн\w*|\bбеспрецедентн\w*|\bбесшовн\w*"
     r"|\bнезабываем\w*|\bв самом сердце\b|\bнастоящ\w* (?:прорыв|революци|находк|маги|праздник)\w*"
     r"|\bшироки\w* (?:спектр|ассортимент|выбор)\w*|\bмир (?:возможностей|вкуса|вкусов|красоты|удовольствия)\b"
     r"|\bгармоничн\w* сочета\w*|\bидеальн\w* (?:решение|выбор|вариант)\b|\bключев\w*|\bважнейш\w*|\bвысочайш\w*",
     "inflated word: one is fine, several make a brochure", "use the plain word or the concrete detail")
rule("filler.ru-importance", HARD, "ru", "anywhere",
     r"\bважно (?:отметить|подчеркнуть|понимать,? что|помнить,? что)\b",
     "announces importance instead of showing it", "state the thing")
rule("filler.ru-phrase", SOFT, "ru", "anywhere",
     r"\b(?:стоит|следует|необходимо|нужно) (?:отметить|подчеркнуть|упомянуть)\b|\bкогда речь (?:ид[её]т|заходит) о\b"
     r"|\bв наше время\b|\bна сегодняшний день\b|\bв настоящее время\b|\bпо большому сч[её]ту\b"
     r"|\bв конечном (?:сч[её]те|итоге)\b|\bпо сути(?: дела)?\b|\bкак (?:известно|все мы знаем|мы все знаем)\b"
     r"|\bвсе мы (?:знаем|понимаем|помним|сталкивались)\b|\bи последнее,? но не по важности\b|\bкак ни крути\b",
     "filler phrase", "cut it or say the specific thing")
rule("officialese.ru", SOFT, "ru", "anywhere",
     r"\bявля(?:ется|ются|лся|лась|лись)\b|\bпредставля(?:ет|ют) собой\b|\bвыступа(?:ет|ют) в (?:роли|качестве)\b"
     r"|\bслуж(?:ит|ат) (?:для|в качестве)\b|\bосуществля\w*"
     r"|\bпроизвод(?:ится|ятся|ит) (?:оплат|доставк|ремонт|запис|замен|расч[её]т|обработк)\w*"
     r"|\bв рамках\b|\bв целях\b|\bс целью\b|\bпосредством\b"
     r"|\bданн(?:ый|ая|ое|ые|ого|ой|ую|ым|ом|ых)\s+(?:продукт|товар|услуг|сервис|материал|вопрос|момент|случа|приложени"
     r"|решени|метод|подход|стать)\w*"
     r"|\bв связи с (?:тем|чем)\b|\bимеет место\b|\bв случае необходимости\b|\bввиду\b|\bвышеуказанн\w*",
     "officialese (канцелярит)", "use the plain verb or 'это': 'мы делаем', 'это'")
rule("calque.ru", SOFT, "ru", "anywhere",
     r"\bна (?:ежедневной|регулярной|постоянной|еженедельной) основе\b|\bсделать разницу\b"
     r"|\bрады (?:сообщить|объявить|представить|поделиться)\b|\bс гордостью (?:представляем|сообщаем|объявляем)\b"
     r"|\bэто имеет смысл[.!]|\bдайте (?:мне )?знать\b",
     "calque from English", "say it the Russian way: 'каждый день', 'у нас новость'")

# ---- significance tails -------------------------------------------------------
rule("tail.en", SOFT, "en", "anywhere",
     r",\s+(?:further |thereby )?(?:reinforcing|underscoring|reflecting|signal(?:l)?ing|setting the stage for|paving the way for"
     r"|contributing to|fostering|enabling|allowing for|marking a|cementing|highlighting|showcasing|ensuring)\b"
     r"|,\s+making (?:it|them|this) (?:a|an|the|more|easier|ideal|perfect)\b",
     "significance tail: the sentence ends, then explains why it matters", "cut the tail, or give the point its own sentence")
rule("tail.ru", SOFT, "ru", "anywhere",
     r",\s+(?:тем самым |что )?(?:подч[её]ркивая|подч[её]ркивает|открывая|формируя|укрепляя|способствуя|позволяя|обеспечивая)\b"
     r"|,\s+что делает (?:его|е[её]|их|это|процесс|работу)\b|,\s+тем самым\b",
     "significance tail: the sentence ends, then explains why it matters", "cut the tail, or give the point its own sentence")
# ---- trendy decorations, vague declaratives, telling instead of showing -----
rule("trend.en", SOFT, "en", "anywhere",
     r"\bquietly (?:revolutioniz|transform|reshap|chang|becom|redefin)\w*|\bthe quiet (?:revolution|art) of\b|\bload-bearing\b"
     r"|\bdoing (?:the|all the) heavy lifting\b|\ba masterclass in\b|\blives rent[- ]free\b|\bhits different\b"
     r"|\bchef" + AP + r"s kiss\b|\bmain character energy\b|\bthe unglamorous (?:truth|part|work)\b|\ba love letter to\b"
     r"|\bit" + AP + r"s giving\b",
     "fashionable decoration", "drop the ornament and state the claim")
rule("trend.ru", SOFT, "ru", "anywhere",
     r"\bтих(?:ая|ую|ой) революци\w*|\bтихо (?:меняет|захватывает|переписывает)\b|\bэто база\b|\bмастхэв\w*"
     r"|\bвау-эффект\w*|\bбез воды\b",
     "fashionable decoration", "drop the ornament and state the claim")
rule("vague.en", SOFT, "en", "anywhere",
     r"\bthe (?:impact|potential) (?:is|will be|could be) (?:huge|enormous|massive|profound|limitless|immense)\b"
     r"|\bthe possibilities are (?:endless|limitless)\b|\bthis is (?:bigger|deeper) than (?:it (?:seems|looks)|you think)\b"
     r"|\bchanges the game\b|\bthe future is (?:here|now|bright)\b|\bmore than ever\b",
     "says that something is big without saying what", "name the consequence, or cut the sentence")
rule("vague.ru", SOFT, "ru", "anywhere",
     r"\bвозможности (?:безграничны|бесконечны)\b|\bпотенциал (?:огромен|колоссален|безграничен)\b"
     r"|\bбудущее уже (?:здесь|наступило)\b|\bменяет правила игры\b|\bвс[её] не так просто,? как кажется\b"
     r"|\bкак никогда (?:раньше|важно|актуально)\b|\bпочему это важно\b",
     "says that something is big without saying what", "name the consequence, or cut the sentence")
rule("rhythm.parallel", SOFT, "any", "anywhere",
     r"\bthe one [^.!?\n]{1,40}?, the one [^.!?\n]{1,40}?,? (?:and )?the one\b"
     r"|\bт(?:от|та|те),? (?:кто|что|который|которая) [^.!?\n]{1,40}?, т(?:от|та|те),? (?:кто|что|который|которая)\b",
     "parallel triplet ('the one who..., the one who...')", "vary the syntax or keep the strongest item")

# ---- jargon ----------------------------------------------------------------
rule("jargon.en", SOFT, "en", "anywhere",
     r"\bcircle back\b|\btouch base\b|\bmove the needle\b|\blow-hanging fruit\b|\bdeep[- ]dive\b|\bdouble down\b"
     r"|\blean(?:ing)? into\b|\bsynerg(?:y|ies|istic)\b|\bbandwidth\b|\btake (?:this|it) offline\b"
     r"|\bactionable (?:insights|steps|takeaways)\b|\bvalue[- ]add\b|\bwin-win\b|\bthink outside the box\b"
     r"|\bboil the ocean\b|\bnorth star\b|\bpain points?\b|\blearnings\b|\bbest practices\b"
     r"|\bdrive (?:growth|engagement|results|value|impact|innovation)\b|\bkey stakeholders\b|\b(?:going|moving) forward\b"
     r"|\bon the same page\b|\bmission-critical\b|\bcore competenc(?:y|ies)\b|\bthought leader(?:ship)?\b|\blevel up\b"
     r"|\bunpack(?:ing)? (?:this|that|the)\b|\bnavigat(?:e|ing) (?:uncertainty|change|challenges)\b",
     "business jargon", "say the plain action: 'call you on Friday', 'fix the signup form'")
rule("jargon.ru", SOFT, "ru", "anywhere",
     r"\bсинерги\w*|\bпрокач(?:ать|ай|айте|ивать|ивает|ка|ки|ку)\w*|\bдрайв(?:ить|ит|ят|им)\b|\bчеллендж\w*"
     r"|\bзакры(?:ть|ваем|вает|ли) (?:боль|потребност)\w*|\bв разрезе\b|\bпо итогу\b"
     r"|\b(?:новые|серь[её]зные|главные|основные|ключевые|современные) вызовы\b|\bточк\w* роста\b"
     r"|\bбол(?:и|ей|ь) (?:клиент|аудитори|пользовател|бизнес)\w*|\bинсайт\w*|\bфидб[еэ]к\w*"
     r"|\bапрув\w*|\bзасинк\w*|\bзафинал\w*|\bна одной волне\b|\bзон\w* комфорта\b|\bлучши\w* практик\w*"
     r"|\bценностн\w* предложени\w*",
     "business jargon", "say the plain action")

# ---- hedging ---------------------------------------------------------------
rule("hedge.double", HARD, "en", "anywhere",
     r"\b(?:may|might|could) (?:potentially|possibly|perhaps|conceivably)\b|\b(?:perhaps|maybe|possibly) (?:might|may|could)\b",
     "stacked hedge", "keep one hedge, and only where the doubt is real")
rule("hedge.ru-double", HARD, "ru", "anywhere",
     r"\bвозможно,? (?:может быть|вероятно|скорее всего|могло бы)\b|\bможет быть,? (?:возможно|вероятно)\b"
     r"|\bвероятно,? (?:возможно|может быть)\b|\bпо всей видимости,? (?:возможно|вероятно)\b"
     r"|\bвозможно,? в (?:какой-то|некоторой|определ[её]нной) (?:мере|степени)\b",
     "stacked hedge", "keep one hedge, and only where the doubt is real")
rule("hedge.voiceless", SOFT, "en", "anywhere",
     r"\bsome (?:might|may|would) (?:say|argue)\b|\bone (?:could|might) argue\b|\bit could be argued\b|\barguably\b",
     "hedge that hides who holds the view", "say who thinks it, or say it yourself")
rule("hedge.ru-voiceless", SOFT, "ru", "anywhere",
     r"\bкто-то (?:скажет|возразит)\b|\bможно (?:возразить|поспорить|сказать,? что)\b|\bне исключено,? что\b",
     "hedge that hides who holds the view", "say who thinks it, or say it yourself")

# ---- emphasis crutches, permission slips, fake intimacy --------------------
rule("crutch.en", HARD, "en", "anywhere",
     r"\blet that sink in\b|\bread that (?:again|twice)\b|\bthis changes everything\b|\bsay it louder\b"
     r"|\bi can(?:not|" + AP + r"t) stress (?:this|enough)\b|\band (?:that|this)" + AP + r"s (?:okay|ok|fine|normal)\b",
     "emphasis crutch or permission slip", "cut it; if the point needs help, sharpen the point")
rule("crutch.en-stop", HARD, "en", "sentence", r"(?:full stop|period|end of story|i" + AP + r"ll wait)[.!]\s*\Z",
     "one-word emphasis sentence", "cut it")
rule("intimacy.en", SOFT, "en", "anywhere",
     r"\bmark my words\b|\bthis is huge\b|\byou" + AP + r"re not alone\b|\byou" + AP + r"ve got this\b"
     r"|\btrust me\b|\bbetween you and me\b|\breal talk\b|\blet me let you in on\b"
     r"|\bhere" + AP + r"s a (?:little )?secret\b|\bi" + AP + r"m going to be vulnerable\b",
     "fake intimacy or emphasis crutch", "keep only if it is literally true for this author")
rule("crutch.ru", HARD, "ru", "anywhere",
     r"\bвдумайтесь\b|\bперечитайте (?:это )?(?:ещ[её] раз|дважды|медленно)\b|\bэто меняет вс[её]\b"
     r"|\bи это (?:нормально|окей|ок)\b",
     "emphasis crutch or permission slip", "cut it")
rule("crutch.ru-stop", HARD, "ru", "sentence", r"(?:точка|и точка|без вариантов)[.!]\s*\Z",
     "one-word emphasis sentence", "cut it")
rule("intimacy.ru", SOFT, "ru", "anywhere",
     r"\bзадумайтесь\b|\bвы не одни\b|\bповерьте(?: мне)?[,!.]|\bмежду нами\b|\bоткрою (?:вам )?(?:секрет|тайну)\b"
     r"|\bскажу по секрету\b|\bположа руку на сердце\b|\bбуду с вами (?:откровенен|откровенна|честен|честна)\b"
     r"|\bдавайте начистоту\b",
     "fake intimacy or emphasis crutch", "keep only if it is literally true for this author")
# ---- recaps and morals -----------------------------------------------------
rule("recap.marker", HARD, "en", "sentence",
     r"(?:in conclusion|in summary|to sum (?:it )?up|to summarize|to recap|in closing)\b",
     "recap marker: the reader has just read it", "end on the last new point or the ask")
rule("recap.soft", SOFT, "en", "sentence",
     r"(?:all in all|in a nutshell|in short|overall|ultimately|the bottom line(?: is)?|bottom line|key takeaways?"
     r"|the takeaway(?: here)? is|long story short)\b",
     "summary move", "check that it adds something new; otherwise end before it")
rule("recap.ru-marker", HARD, "ru", "sentence",
     r"(?:в заключение|подводя (?:итог|итоги|черту)|подытож(?:им|у|ивая)|резюмируя|резюмир(?:ую|уем)"
     r"|если подытожить|если резюмировать)\b",
     "recap marker: the reader has just read it", "end on the last new point or the ask")
rule("recap.ru-soft", SOFT, "ru", "sentence",
     r"(?:в итоге|таким образом|в сухом остатке|главный вывод|ключевые выводы|короче говоря|одним словом)\b",
     "summary move", "check that it adds something new")

# ---- staged reveals: "The result? ..." -------------------------------------
_REVEAL_EN = (r"(?:the )?(?:result|results|answer|secret|catch|kicker|twist|best part|worst part|problem|reason"
              r"|lesson|takeaway|outcome|truth|verdict|difference|fix|solution|trick|upshot|irony|punchline)")
_REVEAL_RU = r"(?:результат|итог|ответ|секрет|причина|подвох|вывод|решение|разница|фишка|мораль|суть|ирония|соль)"
rule("reveal.qa", HARD, "en", "anywhere",
     SS + _REVEAL_EN + r"\?(?=\s+\S)|\bwhy\?\s+because\b|\bhow\?\s+by\b",
     "question asked and answered at once: a staged reveal", "state the result in one sentence")
rule("reveal.ru-qa", HARD, "ru", "anywhere",
     SS + _REVEAL_RU + r"\?(?=\s+\S)|\bпочему\?\s+(?:да )?потому\b|\bзачем\?\s+(?:чтобы|затем)\b|\bкак\?\s+(?:очень )?просто\b",
     "question asked and answered at once: a staged reveal", "state the result in one sentence")
rule("reveal.colon", SOFT, "en", "anywhere",
     SS + r"(?:the |my |our |here" + AP + r"s the )?(?:result|lesson|takeaway|kicker|catch|twist|truth|point|secret"
     r"|answer|bottom line|upshot|verdict|moral|irony)\s*:\s+\S",
     "label-colon reveal", "one per text at most; fold it into a sentence")
rule("reveal.ru-colon", SOFT, "ru", "anywhere",
     SS + r"(?:итог|результат|вывод|мораль|суть|секрет|фишка|подвох|ответ|главное|ирония)\s*:\s+\S",
     "label-colon reveal", "one per text at most; fold it into a sentence")
rule("cleft.en", SOFT, "en", "sentence",
     r"what (?:makes|sets|i love|i like|really matters|matters most|struck me|surprised me|stood out)\b[^.!?\n]{0,60}\b(?:is|was)\b",
     "'What makes X special is' wind-up", "lead with the subject: 'X is special because'")

# ---- X by X ------------------------------------------------------------------
rule("xbyx.en", SOFT, "en", "anywhere",
     r"\b(step|day|week|month|stage|line|page|chapter|module|lesson|section|layer|piece|frame)[- ]by[- ]\1\b",
     "'X by X' promises a full walkthrough", "keep it only if the text goes through each one; else name what the reader gets")
rule("xbyx.ru", SOFT, "ru", "anywhere",
     r"\bшаг за шагом\b|\bнедел\w* за неделей\b|\bдень за дн[её]м\b|\bмесяц за месяцем\b|\bэтап за этапом\b"
     r"|\bстрок\w* за строкой\b|\bстраниц\w* за страницей\b|\bурок за уроком\b|\bслой за слоем\b",
     "'X за X' promises a full walkthrough", "keep it only if the text goes through each one")

# ---- false agency, hidden actors ------------------------------------------
rule("agency.en", SOFT, "en", "anywhere",
     r"\bthe (?:numbers|data|results) speak for themselves\b|\bthe market (?:has spoken|decides|decided)\b"
     r"|\b(?:a|the) decision was (?:made|taken|reached)\b|\bit was decided\b|\bmistakes were made\b"
     r"|\busers are (?:encouraged|advised) to\b|\bfeedback (?:shaped|drove|guided) the\b|\bgrowth followed\b",
     "deed without a doer", "name who did it")
rule("agency.ru", SOFT, "ru", "anywhere",
     r"\bцифры говорят сами за себя\b|\bрынок (?:решил|рассудил|расставил)\w*|\bбыло принято решение\b|\bбыло решено\b"
     r"|\bнаблюдается (?:рост|снижение|тенденция|увеличение)\b|\bпользователям рекомендуется\b|\bрост не заставил себя ждать\b"
     r"|\b(?:было|были|был|была) (?:сделано|создано|отмечено|установлено|выявлено|обнаружено|замечено)\b",
     "deed without a doer", "name who did it")
# ---- ad mode only ------------------------------------------------------------
rule("ad.claim", SOFT, "en", "anywhere",
     r"\b(?:the )?best\b|#1\b|\bno\.? ?1\b|\bnumber one\b|\bleading\b|\btop-rated\b|\bunbeatable\b"
     r"|\bguarantee[ds]?\b|\brisk-free\b|\b100 ?%",
     "claim that needs proof", "keep it only with proof you can show; else make it concrete", ad_only=True)
rule("ad.urgency", SOFT, "en", "anywhere",
     r"\b(?:only today|today only|last chance|hurry|limited time|act now|while (?:stocks|supplies) last"
     r"|don" + AP + r"t miss out|ends (?:tonight|soon)|before it" + AP + r"s too late)\b",
     "urgency", "keep it only if true, with the real date or quantity", ad_only=True)
rule("ad.cta", SOFT, "en", "anywhere", r"\bclick here\b|\blearn more\b",
     "vague call to action", "say what happens after the click", ad_only=True)
rule("ad.ru-claim", SOFT, "ru", "anywhere",
     r"\bлучш(?:ий|ая|ее|ие|ую|его|ей|их)\b|№ ?1\b|\bномер один\b|\bлидер\w* рынка\b"
     r"|\bсам(?:ый|ая|ое|ые|ую|ых) (?:низк|выгодн|быстр|дешёв|дешев|надёжн|надежн|лучш|качественн)\w*"
     r"|\bгарантир\w*|\b100 ?%",
     "claim that needs proof", "keep it only with proof you can show; else make it concrete", ad_only=True)
rule("ad.ru-urgency", SOFT, "ru", "anywhere",
     r"\bтолько сегодня\b|\bпоследний шанс\b|\bуспе(?:й|йте|ть)\b|\bспешите\b|\bосталось (?:всего|только)\b"
     r"|\bпредложение ограничено\b|\bне упустите\b|\bколичество ограничено\b|\bпока не поздно\b",
     "urgency", "keep it only if true, with the real date or quantity", ad_only=True)
rule("ad.ru-cta", SOFT, "ru", "anywhere", r"\bнажмите (?:здесь|сюда)\b|\bподробнее по ссылке\b|\bузнать подробнее\b",
     "vague call to action", "say what happens after the click", ad_only=True)

# ---- clusters (per paragraph) ----------------------------------------------
cluster("intensifiers.en", "en",
        r"\b(?:really|very|truly|genuinely|deeply|incredibly|extremely|absolutely|literally|actually|basically"
        r"|essentially|honestly|simply|just|totally|definitely|certainly|fundamentally|inherently|ultimately"
        r"|clearly|obviously|undoubtedly|super)\b",
        3, "intensifier pile-up", "keep the ones that change the meaning")
cluster("intensifiers.ru", "ru",
        r"\b(?:буквально|реально|действительно|по-настоящему|абсолютно|совершенно|невероятно|безусловно|однозначно"
        r"|очень|просто|честно|по сути|фактически|крайне|максимально|чрезвычайно)\b",
        3, "intensifier pile-up", "keep the ones that change the meaning")
cluster("hedges.en", "en",
        r"\b(?:perhaps|possibly|potentially|arguably|somewhat|relatively|seemingly|apparently|in some (?:ways|cases)"
        r"|in many ways|to (?:some|a certain) (?:extent|degree)|more or less|kind of|sort of|it seems|it appears"
        r"|generally speaking)\b",
        3, "hedge pile-up", "commit; keep one hedge where the doubt is real and say what is uncertain")
cluster("hedges.ru", "ru",
        r"\b(?:возможно|вероятно|пожалуй|отчасти|в какой-то (?:мере|степени)|в некотором (?:смысле|роде)"
        r"|в определ[её]нной (?:мере|степени)|как бы|своего рода|в целом|по-видимому|достаточно|довольно)\b",
        3, "hedge pile-up", "commit; keep one hedge where the doubt is real")
cluster("praise.en", "en",
        r"\b(?:amazing|incredible|awesome|fantastic|wonderful|outstanding|exceptional|remarkable|stunning|phenomenal"
        r"|extraordinary|unbelievable|superb|breathtaking)\b",
        2, "generic praise pile-up", "replace with the detail that earned the praise")
cluster("praise.ru", "ru",
        r"\b(?:потрясающ|невероятн|удивительн|восхитительн|великолепн|шикарн|бесподобн|феноменальн|изумительн"
        r"|волшебн|чудесн|прекрасн)\w*",
        2, "generic praise pile-up", "replace with the detail that earned the praise")
cluster("absolutes.en", "en",
        r"\b(?:everyone|everybody|nobody|no one|always|never|everything|every single|all of us|none of us)\b",
        3, "absolutes pile-up", "narrow the claim to what you know")
cluster("absolutes.ru", "ru",
        r"\b(?:всегда|никогда|никто|ничто|каждый|каждая|каждое|любой|любая|все без исключения|абсолютно все)\b",
        3, "absolutes pile-up", "narrow the claim to what you know")
cluster("passive.en", "en",
        r"\b(?:is|are|was|were|been|being|be)\s+(?:\w+ly\s+)?(?:\w{3,}ed|made|done|given|taken|known|seen|built|written"
        r"|shown|held|found|kept|told|sent|paid|chosen|driven|grown|brought|bought|left|lost|meant|run|set|put)\b",
        3, "passive run: actors keep disappearing", "put the people who act at the front")

# ---- structural checks, listed for --list-rules -----------------------------
STRUCTURAL = [
    ("dash.em", HARD, "any", "em dash used as punctuation (also &mdash;)"),
    ("dash.en", HARD, "any", "spaced en dash between words"),
    ("dash.ascii", HARD, "any", "' -- ' standing in for an em dash"),
    ("dash.range", SOFT, "any", "em dash inside a number range"),
    ("dash.bullet", SOFT, "any", "dash used as a bullet or dialogue mark at line start"),
    ("dash.hyphens", SOFT, "any", "three or more spaced hyphens used as dashes"),
    ("quote.emphasis", HARD, "any", "short lowercase phrase in quotes for emphasis (\"\" “” «» „“ '')"),
    ("fragment.run", HARD, "any", "three or more one- or two-word sentences in a row (SOFT in --ad)"),
    ("fragment.staccato", SOFT, "any", "four or more sentences of four words or fewer in a row"),
    ("contrast.negative-buildup", HARD, "any", "'Not X. Not Y. Z.' build-up"),
    ("rhythm.metronome", SOFT, "any", "four or more sentences of nearly the same length"),
    ("rhythm.one-line-paragraphs", SOFT, "any", "most paragraphs are a single sentence"),
    ("rhythm.punchlines", SOFT, "any", "paragraphs keep ending on a short punchline"),
    ("rhythm.triplets", SOFT, "any", "rule-of-three habit: many 'A, B and C' lists"),
    ("rhythm.the-openers", SOFT, "en", "many sentences open with 'The'"),
    ("rhythm.hyphen-compounds", SOFT, "en", "hyphenated compound modifiers sprinkled through the text"),
    ("list.bold-labels", SOFT, "any", "every list item is '**Label:** explanation'"),
    ("list.uniform", SOFT, "any", "list items of near-identical length"),
    ("list.same-start", SOFT, "any", "every list item starts with the same word"),
    ("list.emoji-bullets", SOFT, "any", "emoji used as bullets"),
    ("layout.headings", SOFT, "any", "headings in a short text"),
    ("layout.bold", SOFT, "any", "bold used for emphasis again and again"),
    ("layout.emoji", SOFT, "any", "dense emoji"),
    ("layout.hashtags", SOFT, "any", "four or more hashtags"),
    ("layout.exclamations", SOFT, "any", "exclamation marks piling up"),
    ("names.cycling", SOFT, "en/ru", "one thing called by many names (app, platform, tool, solution...)"),
    ("recap.paragraph", SOFT, "any", "closing paragraph mostly repeats earlier text"),
    ("closer.question", SOFT, "any", "text ends on a question (fine once, a tell across a batch)"),
    ("closer.stacked", SOFT, "any", "last paragraph stacks either/or questions"),
    ("batch.questions", SOFT, "any", "--batch: too many texts end on a question"),
    ("batch.openers", SOFT, "any", "--batch: texts open the same way"),
]
for _c in CLUSTERS:
    STRUCTURAL.append((_c.rid, SOFT, _c.lang, f"{_c.reason} ({_c.threshold}+ in one paragraph)"))


# --------------------------------------------------------------------------
# Text preparation
# --------------------------------------------------------------------------
FENCE_RX = re.compile(r"^[ \t]*(```|~~~).*?(?:^[ \t]*\1[ \t]*$|\Z)", re.S | re.M)
HTML_COMMENT_RX = re.compile(r"<!--.*?(?:-->|\Z)", re.S)
SCRIPT_RX = re.compile(r"<(script|style)\b.*?</\1\s*>", re.S | re.I)
BLOCKQUOTE_HTML_RX = re.compile(r"<blockquote\b.*?</blockquote\s*>", re.S | re.I)
BLOCKQUOTE_MD_RX = re.compile(r"^[ \t]*>.*$", re.M)
BLOCK_TAG_RX = re.compile(r"</?(?:p|div|li|ul|ol|h[1-6]|br|tr|td|th|table|section|article|header|footer|figure"
                          r"|figcaption|pre|blockquote|main|aside|nav)\b[^<>]*>", re.I)
TAG_RX = re.compile(r"</?[A-Za-z][^<>\n]*>")
INLINE_CODE_RX = re.compile(r"`[^`\n]+`")
URL_RX = re.compile(r"\bhttps?://\S+|\bwww\.\S+")
EMAIL_RX = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
LINK_TARGET_RX = re.compile(r"(?<=\])\([^)\s]*\)")


def mask(text: str, include_quotes: bool, front_matter: bool) -> str:
    """Blank out what is not prose, keeping every offset and newline in place."""
    buf = list(text)

    def blank(a: int, b: int, fill: str = " ") -> None:
        for i in range(a, b):
            if buf[i] != "\n":
                buf[i] = fill

    if front_matter:
        m = re.match(r"---[ \t]*\n.*?\n---[ \t]*(?:\n|\Z)", text, re.S)
        if m:
            blank(m.start(), m.end())
    for rx in (FENCE_RX, HTML_COMMENT_RX, SCRIPT_RX):
        for m in rx.finditer(text):
            blank(m.start(), m.end())
    if not include_quotes:
        for rx in (BLOCKQUOTE_HTML_RX, BLOCKQUOTE_MD_RX):
            for m in rx.finditer(text):
                blank(m.start(), m.end())
    for m in BLOCK_TAG_RX.finditer(text):
        blank(m.start(), m.end(), "\x00")  # paragraph break that keeps offsets
    for rx in (TAG_RX, INLINE_CODE_RX, URL_RX, EMAIL_RX, LINK_TARGET_RX):
        for m in rx.finditer(text):
            blank(m.start(), m.end())
    return "".join(buf)


def detect_languages(text: str) -> set[str]:
    cyr = len(re.findall(r"[а-яёіїєґ]", text, re.I))
    lat = len(re.findall(r"[a-z]", text, re.I))
    uk_letters = len(re.findall(r"[іїєґ]", text, re.I))
    ru_letters = len(re.findall(r"[ыэъё]", text, re.I))
    total = cyr + lat
    langs: set[str] = set()
    if total == 0:
        return {"en"}
    if lat / total >= 0.15:
        langs.add("en")
    if cyr / total >= 0.15:
        langs.add("uk" if uk_letters > ru_letters else "ru")
    if not langs:
        langs.add("en" if lat >= cyr else ("uk" if uk_letters > ru_letters else "ru"))
    return langs


# --------------------------------------------------------------------------
# Segmentation
# --------------------------------------------------------------------------
PARA_BREAK = re.compile(r"\n[ \t\x00]*\n|\x00+")
MARKER_RX = re.compile(r"[ \t]*(?:[-*+•▪◦●–—]|\d{1,3}[.)])[ \t]+")
HEADING_RX = re.compile(r"[ \t]*#{1,6}[ \t]+")
END_RX = re.compile(r"[.!?…]+[\"'»”’)\]*_]*(?=[\s\x00]|$)")
WORD_RX = re.compile(r"[^\W\d_]+(?:[-'’][^\W\d_]+)*|\d+(?:[.,:]\d+)*")
EMOJI_RX = re.compile("[\U0001F000-\U0001FAFF☀-➿⬀-⯿←-⇿]")
ABBR = {"e.g", "i.e", "etc", "vs", "mr", "mrs", "ms", "dr", "st", "approx", "т.е", "т.д", "т.п", "т.к", "др",
        "пр", "им", "ул", "г", "гг", "см", "стр", "руб", "грн", "тыс", "млн", "млрд", "коп", "мин", "сек", "ок"}
BLANKS = " \t\n\x00"


@dataclass
class Sent:
    start: int
    lead: int
    end: int
    para: int
    unit: int
    kind: str  # prose | item | heading
    words: int


@dataclass
class Unit:
    kind: str
    start: int
    end: int
    para: int
    marker: str = ""


def split_spans(text: str, rx: re.Pattern, start: int, end: int):
    pos = start
    for m in rx.finditer(text, start, end):
        if text[pos:m.start()].strip(BLANKS):
            yield pos, m.start()
        pos = m.end()
    if text[pos:end].strip(BLANKS):
        yield pos, end


def units_of(text: str, ps: int, pe: int, para: int) -> list[Unit]:
    units: list[Unit] = []
    cur: Unit | None = None
    pos = ps
    for line in text[ps:pe].split("\n"):
        ls, le = pos, pos + len(line)
        pos = le + 1
        if not line.strip(BLANKS):
            continue
        m = HEADING_RX.match(text, ls, le)
        if m:
            units.append(Unit("heading", m.end(), le, para))
            cur = None
            continue
        m = MARKER_RX.match(text, ls, le)
        if m:
            cur = Unit("item", m.end(), le, para, m.group().strip())
            units.append(cur)
            continue
        if cur is None:
            cur = Unit("prose", ls, le, para)
            units.append(cur)
        else:
            cur.end = le
    return units


def lead_of(text: str, s: int, e: int) -> int:
    i = s
    while i < e and (text[i] in BLANKS + ">*_\"'«“„‘([" or EMOJI_RX.match(text[i])):
        i += 1
    return i


def sentences_of(text: str, u: Unit, uid: int) -> list[Sent]:
    out: list[Sent] = []
    pos = u.start

    def push(a: int, b: int) -> None:
        while a < b and text[a] in BLANKS:
            a += 1
        if a >= b:
            return
        lead = lead_of(text, a, b)
        words = len(WORD_RX.findall(text, lead, b))
        if words:
            out.append(Sent(a, lead, b, u.para, uid, u.kind, words))

    for m in END_RX.finditer(text, u.start, u.end):
        if m.group().startswith(".") and len(m.group().rstrip("\"'»”’)]*_")) == 1:
            j = m.start()
            while j > pos and (text[j - 1].isalpha() or text[j - 1] == "."):
                j -= 1
            tok = text[j:m.start()]
            if tok.lower() in ABBR or (len(tok) == 1 and tok.isupper()):
                continue
        push(pos, m.end())
        pos = m.end()
    push(pos, u.end)
    return out


# --------------------------------------------------------------------------
# Checking
# --------------------------------------------------------------------------
@dataclass
class Hit:
    sev: str
    rid: str
    start: int
    end: int
    reason: str
    fix: str
    note: str = ""
    anchor: bool = False  # the span only marks a place; show the line from there


class Item:
    """One text: the whole input, or one entry of a --batch file."""

    def __init__(self, text: str, start: int, end: int):
        self.start, self.end = start, end
        self.paras = list(split_spans(text, PARA_BREAK, start, end))
        self.units: list[Unit] = []
        for pi, (ps, pe) in enumerate(self.paras):
            self.units.extend(units_of(text, ps, pe, pi))
        self.sents: list[Sent] = []
        for ui, u in enumerate(self.units):
            self.sents.extend(sentences_of(text, u, ui))
        self.prose = [s for s in self.sents if s.kind != "heading"]
        self.words = len(WORD_RX.findall(text, start, end))


def lang_ok(lang: str, langs: set[str]) -> bool:
    if lang == "any":
        return True
    return lang in langs or (lang == "en/ru" and bool(langs & {"en", "ru"}))


def severity(r: Rule, ad: bool) -> str:
    if r.ad_only and not ad:
        return OFF
    if ad and r.ad:
        return r.ad
    return r.sev


def check_rules(t: str, it: Item, langs: set[str], ad: bool, hits: list[Hit]) -> None:
    opening, closing = it.prose[:2], it.prose[-3:]
    for r in RULES:
        if not lang_ok(r.lang, langs):
            continue
        sev = severity(r, ad)
        if sev == OFF:
            continue
        if r.where == "anywhere":
            for m in r.rx.finditer(t, it.start, it.end):
                if m.end() > m.start():
                    hits.append(Hit(sev, r.rid, m.start(), m.end(), r.reason, r.fix))
            continue
        pool = {"sentence": it.sents, "opening": opening, "closing": closing}[r.where]
        for s in pool:
            m = r.rx.match(t, s.lead, s.end)
            if m:
                hits.append(Hit(sev, r.rid, m.start(), max(m.end(), m.start() + 1), r.reason, r.fix))


def check_clusters(t: str, it: Item, langs: set[str], hits: list[Hit]) -> None:
    for c in CLUSTERS:
        if not lang_ok(c.lang, langs):
            continue
        for ps, pe in it.paras:
            found = list(c.rx.finditer(t, ps, pe))
            if len(found) >= c.threshold:
                words = ", ".join(dict.fromkeys(m.group().lower() for m in found))
                hits.append(Hit(SOFT, c.rid, found[0].start(), found[0].end(), c.reason, c.fix,
                                f"{len(found)} in one paragraph: {words}"))


DASH_RX = re.compile(r"—|–|&mdash;|&#8212;|&#x2014;|&ndash;|&#8211;|&#x2013;|(?<=\w) -- (?=\w)", re.I)
SPACED_HYPHEN_RX = re.compile(r"(?<=\w) - (?=\w)")


def check_dashes(t: str, it: Item, hits: list[Hit]) -> None:
    for m in DASH_RX.finditer(t, it.start, it.end):
        a, b, g = m.start(), m.end(), m.group().lower()
        prev = t[a - 1] if a > it.start else ""
        nxt = t[b] if b < len(t) else ""
        line_head = t[t.rfind("\n", 0, a) + 1:a]
        if g.strip() == "--":
            hits.append(Hit(HARD, "dash.ascii", a, b, "' -- ' stands in for an em dash",
                            "comma, period, parentheses, or a new sentence"))
            continue
        em = g in ("—", "&mdash;", "&#8212;", "&#x2014;")
        if not line_head.strip(BLANKS + ">*"):
            hits.append(Hit(SOFT, "dash.bullet", a, b, "dash as a bullet or dialogue mark",
                            "use '-' or a plain list"))
        elif prev.isdigit() and nxt.isdigit():
            if em:
                hits.append(Hit(SOFT, "dash.range", a, b, "em dash in a number range",
                                "a hyphen or 'to' / 'до' reads plainer"))
        elif em:
            hits.append(Hit(HARD, "dash.em", a, b, "em dash used as punctuation",
                            "comma, period, colon, parentheses, or rewrite the sentence"))
        elif prev in " \t\xa0" and nxt in " \t\xa0":
            hits.append(Hit(HARD, "dash.en", a, b, "spaced en dash used as an em dash",
                            "comma, period, parentheses, or rewrite the sentence"))
    spaced = list(SPACED_HYPHEN_RX.finditer(t, it.start, it.end))
    if len(spaced) >= 3:
        m = spaced[0]
        hits.append(Hit(SOFT, "dash.hyphens", m.start(), m.end(), "spaced hyphens used as dashes",
                        "fine once; many look like em dashes swapped by find-and-replace",
                        f"{len(spaced)} in this text"))


QUOTE_RXS = [
    re.compile(r'"([^"\n]{1,80})"'),
    re.compile(r"“([^“”\n]{1,80})”"),
    re.compile(r"«([^«»\n]{1,80})»"),
    re.compile(r"„([^„“”\n]{1,80})[“”]"),
    re.compile(r"(?<![\w'’])['‘]([^\W\d_][^'’\n]{0,60}?)['’](?!\w)"),
]


def check_quotes(t: str, it: Item, hits: list[Hit]) -> None:
    for rx in QUOTE_RXS:
        for m in rx.finditer(t, it.start, it.end):
            body = m.group(1).strip()
            words = WORD_RX.findall(body)
            if not body or not 1 <= len(words) <= 5:
                continue
            first = next((c for c in body if c.isalpha()), "")
            if not first or not first.islower():
                continue  # names, titles, labels and quoted sentences start with a capital
            if body[-1] in ".!?…" and len(words) >= 4:
                continue  # a quoted sentence
            hits.append(Hit(HARD, "quote.emphasis", m.start(), m.end(),
                            "quotes used for emphasis or as a label",
                            "drop the quotes and let the phrase sit in the sentence"))


NEG_START_RX = re.compile(r"(?:not|no|не|ни|ні)\b", re.I)


def check_fragments(t: str, it: Item, ad: bool, hits: list[Hit]) -> None:
    by_unit: dict[int, list[Sent]] = {}
    for s in it.sents:
        if s.kind == "prose":
            by_unit.setdefault(s.unit, []).append(s)
    for ss in by_unit.values():
        i = 0
        while i < len(ss):  # one- or two-word sentences in a row
            j = i
            while j < len(ss) and ss[j].words <= 2:
                j += 1
            if j - i >= 3:
                hits.append(Hit(SOFT if ad else HARD, "fragment.run", ss[i].start, ss[j - 1].end,
                                "string of one- or two-word sentences (manufactured drama)",
                                "write one normal sentence that says how these relate", f"{j - i} in a row"))
                i = j
            else:
                i += 1
        i = 0
        while i < len(ss):  # staccato
            j = i
            while j < len(ss) and ss[j].words <= 4:
                j += 1
            if j - i >= 4:
                hits.append(Hit(SOFT, "fragment.staccato", ss[i].start, ss[j - 1].end,
                                "staccato run of very short sentences", "join the ones that belong together",
                                f"{j - i} in a row"))
                i = j
            else:
                i += 1
        for k in range(len(ss) - 2):  # Not X. Not Y. Z.
            a, b, c = ss[k], ss[k + 1], ss[k + 2]
            if (a.words <= 6 and b.words <= 6 and NEG_START_RX.match(t, a.lead, a.end)
                    and NEG_START_RX.match(t, b.lead, b.end) and not NEG_START_RX.match(t, c.lead, c.end)):
                hits.append(Hit(HARD, "contrast.negative-buildup", a.start, c.end,
                                "'Not X. Not Y. Z.' build-up", "say Z directly"))


def check_rhythm(t: str, it: Item, langs: set[str], hits: list[Hit]) -> None:
    prose_by_para: dict[int, list[Sent]] = {}
    for s in it.sents:
        if s.kind == "prose":
            prose_by_para.setdefault(s.para, []).append(s)
    for ss in prose_by_para.values():  # metronome
        for k in range(len(ss) - 3):
            win = ss[k:k + 4]
            lens = [s.words for s in win]
            if min(lens) >= 6 and max(lens) <= 1.25 * min(lens):
                hits.append(Hit(SOFT, "rhythm.metronome", win[0].start, win[-1].end,
                                "four sentences in a row of nearly the same length",
                                "vary the length the way the content varies",
                                "lengths " + "/".join(map(str, lens))))
                break
    paras = list(prose_by_para.values())
    if len(paras) >= 5 and sum(1 for p in paras if len(p) == 1) >= 0.7 * len(paras):
        hits.append(Hit(SOFT, "rhythm.one-line-paragraphs", paras[0][0].start, paras[0][0].end,
                        "most paragraphs are one sentence (feed-post layout)",
                        "group sentences that belong together into real paragraphs"))
    multi = [p for p in paras if len(p) >= 2]
    punchy = [p for p in multi if p[-1].words <= 5 and p[-2].words >= 10]
    if len(punchy) >= 3 and len(punchy) >= 0.75 * len(multi):
        hits.append(Hit(SOFT, "rhythm.punchlines", punchy[0][-1].start, punchy[0][-1].end,
                        "paragraphs keep ending on a short punchline", "let some paragraphs end on a plain fact",
                        f"{len(punchy)} of {len(multi)} paragraphs"))
    trip_rx = re.compile(r"\b[\w'’-]+(?: [\w'’-]+){0,2}, [\w'’-]+(?: [\w'’-]+){0,2},? "
                         r"(?:and|or|и|или|а также|та|або|і) [\w'’-]+", re.I)
    total, first = 0, None
    for ps, pe in it.paras:
        n = list(trip_rx.finditer(t, ps, pe))
        total += len(n)
        if n and first is None:
            first = n[0]
        if len(n) >= 2:
            first = n[0]
            break
    if first is not None and (total >= 3 or any(len(list(trip_rx.finditer(t, ps, pe))) >= 2 for ps, pe in it.paras)):
        hits.append(Hit(SOFT, "rhythm.triplets", first.start(), first.end(),
                        "rule-of-three habit", "keep the items that matter; two is often enough",
                        f"{total} 'A, B and C' lists"))
    if "en" in langs:
        the = [s for s in it.prose if t.startswith("The ", s.lead)]
        if len(the) >= 3 and len(the) >= 0.35 * len(it.prose):
            hits.append(Hit(SOFT, "rhythm.the-openers", the[0].lead, the[0].lead + 3,
                            "many sentences open with 'The [noun]' (essay voice)",
                            "in casual replies start with the subject, a verb, or 'I'",
                            f"{len(the)} of {len(it.prose)} sentences"))
        ok = {"follow-up", "well-known", "off-label", "day-to-day", "e-mail", "long-term", "short-term",
              "full-time", "part-time", "so-called", "up-to-date", "one-on-one", "face-to-face", "real-time",
              "open-source", "sign-up", "check-in", "log-in", "opt-in", "opt-out", "built-in", "add-on",
              "pop-up", "t-shirt", "x-ray", "co-founder", "re-read", "self-hosted", "year-old"}
        comp = [m for m in re.finditer(r"\b[a-z]+(?:-[a-z]+)+\b", t[:it.end])
                if m.start() >= it.start and m.group() not in ok]
        if len(comp) >= 3 and it.words / len(comp) < 60:
            hits.append(Hit(SOFT, "rhythm.hyphen-compounds", comp[0].start(), comp[0].end(),
                            "hyphenated compound modifiers sprinkled through the text",
                            "spell most of them out in words",
                            ", ".join(dict.fromkeys(m.group() for m in comp[:6]))))


BOLD_LABEL_RX = re.compile(r"(?:\*\*|__)[^*_\n]{1,40}(?:\*\*|__)\s*[:—–-]|(?:\*\*|__)[^*_\n]{1,40}:(?:\*\*|__)")


def check_lists(t: str, it: Item, hits: list[Hit]) -> None:
    by_para: dict[int, list[Unit]] = {}
    for u in it.units:
        if u.kind == "item":
            by_para.setdefault(u.para, []).append(u)
    for items in by_para.values():
        if len(items) < 3:
            continue
        texts = [t[u.start:u.end] for u in items]
        if all(BOLD_LABEL_RX.match(x.lstrip()) for x in texts):
            hits.append(Hit(SOFT, "list.bold-labels", items[0].start, items[0].end,
                            "every item is '**Label:** explanation'",
                            "use plain sentences, or a list without the label ritual"))
        counts = [len(WORD_RX.findall(x)) for x in texts]
        mean = sum(counts) / len(counts)
        if len(items) >= 4 and mean >= 5 and all(abs(c - mean) <= 0.25 * mean for c in counts):
            hits.append(Hit(SOFT, "list.uniform", items[0].start, items[0].end,
                            "list items of near-identical length", "let items be as long as their content",
                            "word counts " + "/".join(map(str, counts))))
        firsts = [WORD_RX.findall(x)[:1] for x in texts]
        if all(firsts) and len({f[0].lower() for f in firsts}) == 1:
            hits.append(Hit(SOFT, "list.same-start", items[0].start, items[0].end,
                            "every item starts with the same word", "vary or drop the repeated word"))
    for ps, pe in it.paras:
        lines = [ln for ln in t[ps:pe].split("\n") if ln.strip(BLANKS)]
        emoji_lines = [ln for ln in lines if EMOJI_RX.match(ln.lstrip(" \t-*•"))]
        if len(emoji_lines) >= 3:
            hits.append(Hit(SOFT, "list.emoji-bullets", ps, ps + 1, "emoji used as bullets",
                            "plain list markers or sentences", anchor=True))


def check_layout(orig: str, t: str, it: Item, ad: bool, hits: list[Hit]) -> None:
    head = next((u for u in it.units if u.kind == "heading"), None)
    if head is not None and it.words < 250:
        hits.append(Hit(SOFT, "layout.headings", head.start, head.end, "headings in a short text",
                        "a post or an email under 250 words rarely needs headings"))
    seg = orig[it.start:it.end]
    bolds = [m for m in re.finditer(r"\*\*[^*\n]+\*\*|__[^_\n]+__|<(?:b|strong)>", seg)]
    if len(bolds) >= 3 and it.words / len(bolds) < 60:
        m = bolds[0]
        hits.append(Hit(SOFT, "layout.bold", it.start + m.start(), it.start + m.end(),
                        "bold used for emphasis again and again", "keep bold for what a skimmer must not miss",
                        f"{len(bolds)} bold spans"))
    emojis = EMOJI_RX.findall(t, it.start, it.end)
    if len(emojis) >= 4 and it.words / len(emojis) < (15 if ad else 30):
        m = EMOJI_RX.search(t, it.start, it.end)
        hits.append(Hit(SOFT, "layout.emoji", m.start(), m.end(), "dense emoji",
                        "keep the ones that carry meaning", f"{len(emojis)} emoji"))
    tags = list(re.finditer(r"(?<![\w#])#[^\W\d_][\w]*", t[:it.end]))
    tags = [m for m in tags if m.start() >= it.start]
    if len(tags) >= 4:
        hits.append(Hit(SOFT, "layout.hashtags", tags[0].start(), tags[0].end(), "hashtag pile",
                        "one to three that people actually search", f"{len(tags)} hashtags"))
    bangs = [m.start() for m in re.finditer(r"!", t[:it.end]) if m.start() >= it.start]
    double = re.search(r"!!|!\?|\?!", t[it.start:it.end])
    if (len(bangs) >= 3 and it.words < 300) or double:
        pos = it.start + double.start() if double else bangs[0]
        hits.append(Hit(SOFT, "layout.exclamations", pos, pos + 1, "exclamation marks piling up",
                        "keep one at most, where the excitement is real", f"{len(bangs)} in this text"))


NAMES = {
    "en": [r"\bapps?\b", r"\bplatforms?\b", r"\btools?\b", r"\bsolutions?\b", r"\bservices?\b", r"\bproducts?\b",
           r"\bsystems?\b", r"\bsoftware\b"],
    "ru": [r"\bприложени\w*", r"\bплатформ\w*", r"\bсервис\w*", r"\bрешени\w*", r"\bинструмент\w*",
           r"\bпродукт\w*", r"\bсистем\w*"],
}


def check_names(t: str, it: Item, langs: set[str], hits: list[Hit]) -> None:
    for lang, pats in NAMES.items():
        if lang not in langs:
            continue
        found = []
        for p in pats:
            m = re.search(p, t[it.start:it.end], re.I)
            if m:
                found.append((m.start() + it.start, m.group().lower()))
        if len(found) >= 4:
            found.sort()
            hits.append(Hit(SOFT, "names.cycling", found[0][0], found[0][0] + len(found[0][1]),
                            "one thing called by many names?", "if they are the same thing, pick one name and repeat it",
                            ", ".join(n for _, n in found)))


STOP = {"about", "which", "there", "their", "these", "those", "would", "could", "should", "because", "really",
        "other", "where", "while", "after", "before", "being", "every", "through", "который", "которые",
        "которая", "потому", "только", "сейчас", "можно", "нужно", "очень", "когда", "чтобы", "этого", "такой",
        "также", "всего", "после", "между", "более", "менее", "через", "здесь", "своих", "своей", "этот"}


def check_recap(t: str, it: Item, hits: list[Hit]) -> None:
    prose_paras = sorted({s.para for s in it.prose})
    if len(prose_paras) < 3:
        return
    last = prose_paras[-1]
    ps, pe = it.paras[last]
    last_words = [w.lower() for w in WORD_RX.findall(t, ps, pe)]
    if len(last_words) < 12:
        return
    earlier = {w.lower()[:5] for w in WORD_RX.findall(t, it.start, ps) if len(w) >= 5}
    stems = [w[:5] for w in last_words if len(w) >= 5 and w not in STOP and not w.isdigit()]
    if len(stems) < 5:
        return
    new_numbers = set(re.findall(r"\d+", t[ps:pe])) - set(re.findall(r"\d+", t[it.start:ps]))
    share = sum(1 for s in stems if s in earlier) / len(stems)
    if share >= 0.6 and not new_numbers:
        hits.append(Hit(SOFT, "recap.paragraph", ps, ps + 1,
                        "closing paragraph mostly repeats earlier text",
                        "end on the last new point or the ask", f"{round(share * 100)}% of its words appeared above",
                        anchor=True))


def check_closing(t: str, it: Item, ad: bool, hits: list[Hit]) -> None:
    if not it.prose:
        return
    last = it.prose[-1]
    tail = t[last.lead:last.end].rstrip(BLANKS + "\"'»”’)*_")
    tail = EMOJI_RX.sub("", tail).rstrip()
    if not ad and tail.endswith("?"):
        hits.append(Hit(SOFT, "closer.question", last.lead, last.end, "ends on a question",
                        "fine once; across several texts it becomes a signature"))
    ps, pe = it.paras[last.para]
    if t.count("?", ps, pe) >= 2 and re.search(r"\b(?:or|или|або)\b", t[ps:pe], re.I):
        hits.append(Hit(SOFT, "closer.stacked", ps, ps + 1, "either/or questions stacked at the end",
                        "ask one open question, or end on what you still don't know", anchor=True))


def ends_with_question(t: str, it: Item) -> bool:
    if not it.prose:
        return False
    s = it.prose[-1]
    return EMOJI_RX.sub("", t[s.lead:s.end]).rstrip(BLANKS + "\"'»”’)*_").endswith("?")


def check_batch(t: str, items: list[Item], hits: list[Hit]) -> None:
    n = len(items)
    if n < 3:
        return
    q = sum(1 for it in items if ends_with_question(t, it))
    if q > max(1, n // 3):
        hits.append(Hit(SOFT, "batch.questions", items[0].start, items[0].start + 1,
                        "too many texts in the set end on a question",
                        "end most of them on a statement", f"{q} of {n}", anchor=True))
    firsts = []
    for it in items:
        if it.prose:
            w = WORD_RX.findall(t, it.prose[0].lead, it.prose[0].end)
            if w:
                firsts.append(w[0].lower())
    if firsts:
        word, count = Counter(firsts).most_common(1)[0]
        if count >= 3 and count >= 0.5 * n:
            hits.append(Hit(SOFT, "batch.openers", items[0].start, items[0].start + 1,
                            "texts in the set open the same way", "vary the first move",
                            f"{count} of {n} start with '{word}'", anchor=True))


def split_items(t: str, batch: bool) -> list[tuple[int, int]]:
    if not batch:
        return [(0, len(t))]
    spans, pos = [], 0
    for m in re.finditer(r"^[ \t]*(?:-{3,}|={3,}|\*{3,})[ \t]*$", t, re.M):
        if t[pos:m.start()].strip(BLANKS):
            spans.append((pos, m.start()))
        pos = m.end()
    if t[pos:].strip(BLANKS):
        spans.append((pos, len(t)))
    return spans or [(0, len(t))]


def analyse(text: str, lang: str, ad: bool, batch: bool, include_quotes: bool) -> dict:
    t = mask(text, include_quotes, front_matter=not batch)
    langs = detect_languages(t) if lang == "auto" else {lang}
    hits: list[Hit] = []
    items = [Item(t, a, b) for a, b in split_items(t, batch)]
    for it in items:
        check_rules(t, it, langs, ad, hits)
        check_clusters(t, it, langs, hits)
        check_dashes(t, it, hits)
        check_quotes(t, it, hits)
        check_fragments(t, it, ad, hits)
        check_rhythm(t, it, langs, hits)
        check_lists(t, it, hits)
        check_layout(text, t, it, ad, hits)
        check_names(t, it, langs, hits)
        check_recap(t, it, hits)
        check_closing(t, it, ad, hits)
    if batch:
        check_batch(t, items, hits)

    # One hit per spot: keep the most severe, then the first rule.
    best: dict[int, Hit] = {}
    for h in hits:
        cur = best.get(h.start)
        if cur is None or (cur.sev == SOFT and h.sev == HARD):
            best[h.start] = h
    final = sorted(best.values(), key=lambda h: (h.sev != HARD, h.start))

    line_starts = [0] + [i + 1 for i, c in enumerate(text) if c == "\n"]

    def where(off: int) -> tuple[int, int]:
        lo, hi = 0, len(line_starts) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if line_starts[mid] <= off:
                lo = mid
            else:
                hi = mid - 1
        return lo + 1, off - line_starts[lo] + 1

    out = []
    for h in final:
        line, col = where(h.start)
        end = h.end
        if h.anchor:
            end = text.find("\n", h.start)
            end = len(text) if end < 0 else end
        snippet = re.sub(r"\s+", " ", text[h.start:end]).strip()
        if len(snippet) > 60:
            snippet = snippet[:57] + "..."
        out.append({"severity": h.sev, "rule": h.rid, "line": line, "col": col, "match": snippet,
                    "reason": h.reason, "fix": h.fix, "note": h.note})
    notes = []
    if "uk" in langs:
        notes.append("Ukrainian: only language-neutral checks ran (dashes, quotes, fragments, rhythm, layout). "
                     "Review the wording with references/checklist.md.")
    return {
        "languages": sorted(langs),
        "mode": "ad" if ad else "standard",
        "texts": len(items),
        "words": sum(it.words for it in items),
        "summary": {"hard": sum(1 for h in out if h["severity"] == HARD),
                    "soft": sum(1 for h in out if h["severity"] == SOFT)},
        "notes": notes,
        "hits": out,
    }


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def print_rules() -> None:
    print(f"{'rule':30} {'sev':5} {'ad':5} {'lang':5} {'where':9} reason")
    for r in RULES:
        ad = "only" if r.ad_only else (r.ad or "")
        print(f"{r.rid:30} {r.sev:5} {ad:5} {r.lang:5} {r.where:9} {r.reason}")
    for rid, sev, lang, reason in STRUCTURAL:
        ad = "SOFT" if rid == "fragment.run" else ""
        print(f"{rid:30} {sev:5} {ad:5} {lang:5} {'structure':9} {reason}")


def print_report(source: str, res: dict, hard_only: bool) -> None:
    langs = "+".join(res["languages"])
    print(f"clean-copy check: {source}  [lang {langs} | {res['mode']} | {res['words']} words"
          + (f" | {res['texts']} texts" if res["texts"] > 1 else "") + "]")
    for note in res["notes"]:
        print(f"note: {note}")
    for sev in (HARD, SOFT):
        if sev == SOFT and hard_only:
            break
        group = [h for h in res["hits"] if h["severity"] == sev]
        if not group:
            continue
        print(f"\n{sev} ({len(group)})")
        for h in group:
            extra = f" ({h['note']})" if h["note"] else ""
            pos = f"{h['line']}:{h['col']}"
            print(f"  {pos:<8} {h['rule']:<26} {h['match']!r}")
            print(f"           {h['reason']}{extra}; fix: {h['fix']}")
    s = res["summary"]
    verdict = "fix every HARD hit, then run the checklist" if s["hard"] else \
        "no HARD hits; now run the checklist (the linter cannot judge meaning)"
    print(f"\nsummary: {s['hard']} HARD, {s['soft']} SOFT -> {verdict}")


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(
        prog="clean_copy_check.py",
        description="First-pass linter for machine-sounding prose (Russian and English).",
        epilog="Exit status: 0 no HARD hits, 1 HARD hits found, 2 usage or input error.")
    ap.add_argument("path", nargs="?", help="file to check, or - for standard input")
    ap.add_argument("--text", help="check this text instead of a file")
    ap.add_argument("--lang", choices=["auto", "en", "ru", "uk"], default="auto")
    ap.add_argument("--ad", action="store_true", help="advertising copy rules")
    ap.add_argument("--batch", action="store_true", help="several texts separated by ---, === or *** lines")
    ap.add_argument("--quotes", action="store_true", help="also check blockquote lines")
    ap.add_argument("--hard-only", action="store_true", help="print HARD hits only")
    ap.add_argument("--json", action="store_true", help="JSON output")
    ap.add_argument("--list-rules", action="store_true", help="print every rule and exit")
    args = ap.parse_args(argv)

    if args.list_rules:
        print_rules()
        return 0
    if args.text is not None:
        text, source = args.text, "<text>"
    elif args.path == "-":
        text, source = sys.stdin.read(), "<stdin>"
    elif args.path:
        try:
            with open(args.path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError as exc:
            print(f"clean_copy_check: cannot read {args.path}: {exc.strerror}", file=sys.stderr)
            return 2
        source = args.path
    else:
        ap.print_usage(sys.stderr)
        print("clean_copy_check: give a file, - for stdin, or --text", file=sys.stderr)
        return 2

    res = analyse(text, args.lang, args.ad, args.batch, args.quotes)
    if args.json:
        print(json.dumps({"source": source, **res}, ensure_ascii=False, indent=2))
    else:
        print_report(source, res, args.hard_only)
    return 1 if res["summary"]["hard"] else 0


if __name__ == "__main__":
    sys.exit(main())

"""Rule-based brand checking engine (no external service required).

Uses lightweight NLP: sentence segmentation, tokenisation, lexicons, regex
patterns, fuzzy matching (difflib) and a Flesch readability estimate.

Design principles
-----------------
* Never state that a factual claim is *false* unless it conflicts with an
  approved fact, and even then it is labelled "Conflicts with approved fact".
* Anything that cannot be verified is labelled "Needs Human Verification".
* The engine only suggests. It never approves or publishes anything.
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from typing import Callable, Optional

SEV_WEIGHT = {"High": 15, "Medium": 7, "Low": 3}

SCORE_EXPLANATION = (
    "The Brand Compliance Score is a simple indicative number, not a certified or objective measure of "
    "brand quality. It starts at 100 and subtracts points for each issue found by the automated checks: "
    "High = 15, Medium = 7, Low = 3. Issues that only need human verification count half. The score is "
    "never below 0. It only reflects the guidelines currently saved for your organisation and the limits of "
    "automated checking. A human reviewer always makes the final decision."
)


@dataclass
class Finding:
    title: str
    category: str
    severity: str
    original_text: str
    explanation: str
    guideline: str
    suggestion: str                      # human-readable advice
    suggested_text: Optional[str] = None  # replacement for original_text (None = none available)
    related_source: str = ""
    verification: str = "none"           # none | needs_human_verification | conflicts_with_approved_fact
    source: str = "rule"                 # rule | ai
    rule_id: str = ""
    span: Optional[tuple] = None         # internal: (start, end); used to prevent overlapping findings
    pos: int = 10 ** 9                   # internal: position in text, for ordering


@dataclass
class BrandProfile:
    company_name: str = ""
    tone_style: str = "professional"
    voice: str = ""
    description: str = ""
    approved_terms: list = field(default_factory=list)   # [(preferred, [variants])]
    prohibited: list = field(default_factory=list)       # [(word, replacement|None)]
    claims: list = field(default_factory=list)           # [(phrase, replacement|None)]
    colours: list = field(default_factory=list)          # ["#RRGGBB"]
    slogans: list = field(default_factory=list)
    facts: list = field(default_factory=list)            # [(statement, source)]
    guideline_titles: list = field(default_factory=list)
    document_text: str = ""


# --------------------------------------------------------------------------- parsing helpers
def parse_lines(text: str) -> list:
    return [ln.strip() for ln in (text or "").replace("\r", "").split("\n") if ln.strip()]


def parse_pairs(text: str, sep: str = "=>") -> list:
    out = []
    for ln in parse_lines(text):
        if sep in ln:
            left, right = ln.split(sep, 1)
            out.append((left.strip(), right.strip() or None))
        else:
            out.append((ln, None))
    return [p for p in out if p[0]]


def parse_terms(text: str) -> list:
    out = []
    for ln in parse_lines(text):
        if "|" in ln:
            pref, variants = ln.split("|", 1)
            vs = [v.strip() for v in variants.split(",") if v.strip()]
        else:
            pref, vs = ln, []
        if pref.strip():
            out.append((pref.strip(), vs))
    return out


HEX_RE = re.compile(r"#?[0-9A-Fa-f]{6}\b|#[0-9A-Fa-f]{3}\b")


def parse_colours(text: str) -> list:
    cols = []
    for m in HEX_RE.findall(text or ""):
        h = m.lstrip("#").upper()
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        cols.append("#" + h)
    return list(dict.fromkeys(cols))


def build_profile(guidelines, facts=None, document_text: str = "") -> BrandProfile:
    """Merge all active guidelines of an organisation into one profile."""
    p = BrandProfile()
    for g in guidelines:
        p.guideline_titles.append(g.title)
        p.company_name = p.company_name or g.company_name
        p.description = p.description or (g.description or "")
        if not p.voice:
            p.voice = g.voice_tone or ""
            p.tone_style = g.tone_style or "professional"
        p.approved_terms += parse_terms(g.approved_terms)
        p.prohibited += parse_pairs(g.prohibited_words)
        p.claims += parse_pairs(g.unsupported_claims)
        p.colours += parse_colours(g.brand_colours)
        p.slogans += parse_lines(g.slogans)
        for f in g.facts:
            p.facts.append((f.statement, f.source or ""))
        if g.document and g.document.extracted_text:
            p.document_text += g.document.extracted_text[:6000] + "\n"
    if facts:
        p.facts += facts
    p.colours = list(dict.fromkeys(p.colours))
    return p


# --------------------------------------------------------------------------- text utilities
SENT_RE = re.compile(r"[^.!?\n]+(?:[.!?]+|\n|$)")


def sentences(text: str):
    """Yield (sentence, start, end) with surrounding whitespace trimmed."""
    for m in SENT_RE.finditer(text):
        raw = m.group(0)
        s = raw.strip()
        if not s:
            continue
        start = m.start() + (len(raw) - len(raw.lstrip()))
        yield s, start, start + len(s)


WORD_RE = re.compile(r"[A-Za-z][A-Za-z'’\-]*")
STOP = set("""a an the and or but of in on at to for from with by is are was were be been being this that these those
it its as we our you your they their he she his her i me my us not no yes can will would should could may might
more most than then so such also into over under about after before up down out off very all any each every
has have had do does did who whom which what when where why how there here""".split())


def _stem(w: str) -> str:
    for suf in ("ing", "ed"):
        if len(w) > len(suf) + 3 and w.endswith(suf):
            return w[: -len(suf)]
    if len(w) > 4 and w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    return w


def tokens(text: str, exclude=frozenset()) -> list:
    out = []
    for w in WORD_RE.findall(text):
        lw = w.lower()
        if len(lw) >= 3 and lw not in STOP and lw not in exclude:
            out.append(_stem(lw))
    return out


def match_case(original: str, replacement: str) -> str:
    if not replacement:
        return replacement
    if original[:1].isupper() and replacement[:1].islower():
        return replacement[0].upper() + replacement[1:]
    return replacement


def _overlaps(span, spans) -> bool:
    return any(span[0] < e and s < span[1] for s, e in spans)


def _flesch(text: str) -> float:
    words = WORD_RE.findall(text)
    sents = [s for s, _, _ in sentences(text)]
    if not words or not sents:
        return 100.0

    def syl(w):
        w = w.lower()
        groups = re.findall(r"[aeiouy]+", w)
        n = len(groups)
        if w.endswith("e") and n > 1:
            n -= 1
        return max(1, n)

    syllables = sum(syl(w) for w in words)
    return 206.835 - 1.015 * (len(words) / len(sents)) - 84.6 * (syllables / len(words))


# --------------------------------------------------------------------------- built-in lexicons
SLANG = {
    "gonna": "going to", "wanna": "want to", "gotta": "have to", "lol": "", "omg": "", "btw": "by the way",
    "dude": "", "guys": "everyone", "ya": "you", "yeah": "yes", "nope": "no", "awesome": "excellent",
    "super cool": "impressive", "stuff": "items", "kinda": "somewhat", "sorta": "somewhat", "cheers mate": "",
    "wtf": "", "tbh": "honestly", "asap": "as soon as possible", "hey there": "Hello",
}
CONTRACTION_MAP = {"can't": "cannot", "won't": "will not", "don't": "do not", "doesn't": "does not",
                   "isn't": "is not", "aren't": "are not", "didn't": "did not", "wouldn't": "would not",
                   "shouldn't": "should not", "couldn't": "could not", "we're": "we are", "you're": "you are",
                   "they're": "they are", "we'll": "we will", "you'll": "you will", "we've": "we have",
                   "it's": "it is", "that's": "that is", "let's": "let us", "i'm": "I am"}
PRESSURE = {
    "buy now": "Explore our products", "act now": "Get in touch", "order now": "Learn more",
    "hurry": "Take a look", "don't miss out": "Take a look", "dont miss out": "Take a look",
    "limited time only": "for a limited period", "last chance": "final opportunity",
    "what are you waiting for": "Find out more",
}
EMOJI_RE = re.compile("[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F2FF]")
ACRONYM_WHITELIST = {"PDF", "HTML", "HTTP", "HTTPS", "USA", "INDIA", "FAQ", "CEO", "CTO", "SaaS", "API", "ISO", "UPI", "GST"}

SUPERLATIVE_REPL = {"best": "high-quality", "greatest": "high-quality", "leading": "trusted",
                    "fastest": "fast", "safest": "safe", "#1": "trusted"}
INSTANT_REPL = {"instantly": "promptly", "overnight": "quickly", "effortlessly": "with ease"}
WEAK_CLAIM_REPL = {"best-in-class": "high-quality", "best in class": "high-quality", "unmatched": "strong",
                   "unrivaled": "strong", "unrivalled": "strong", "unbeatable": "strong",
                   "unparalleled": "strong", "second to none": "high-quality", "revolutionary": "innovative",
                   "game-changing": "impactful", "game changing": "impactful", "game-changer": "valuable tool",
                   "cutting-edge": "modern", "cutting edge": "modern", "best-selling": "popular"}


def _repl_worlds(m):
    return SUPERLATIVE_REPL.get(m.group(1).lower(), "high-quality")


def _repl_guarantee(m):
    return "a focus on quality results" if "result" in m.group(0).lower() else "a focus on quality"


def _repl_instant_phrase(_m):
    return "work toward your goals"


def _repl_instant_word(m):
    return INSTANT_REPL.get(m.group(0).lower(), "quickly")


def _repl_weak(m):
    return WEAK_CLAIM_REPL.get(m.group(0).lower(), "high-quality")


def _repl_risk(_m):
    return "designed with care"


# (rule_id, regex, title, severity, verification, replacer, explanation, advice)
PROMO_RULES = [
    ("promo-guarantee",
     re.compile(r"(?:\b\d{2,3}\s?%\s+)?\bguarantee[sd]?\b(?:\s+(?:results?|success|returns?|satisfaction|savings|outcomes?))?", re.I),
     "Absolute guarantee claim", "High", "none", _repl_guarantee,
     "Absolute guarantees promise an outcome that usually cannot be substantiated and may create legal or "
     "regulatory exposure unless explicitly approved.",
     "Remove the guarantee or replace it with a measured statement about your approach."),
    ("promo-worlds-best",
     re.compile(r"(?:\bthe\s+)?\bworld['’]s\s+(best|greatest|leading|fastest|safest)\b", re.I),
     "Unsupported superlative claim", "Medium", "needs_human_verification", _repl_worlds,
     "Superlatives such as \"world's best\" are comparative claims that need documented evidence or approval "
     "before publication. No approved fact in your guidelines supports this wording.",
     "Replace with a factual, measured description, or add approved evidence to the guidelines."),
    ("promo-rank-one",
     re.compile(r"(?<!\w)(?:the\s+)?(?:#\s?1|number\s+one|no\.\s?1)(?!\w)", re.I),
     "Unsupported ranking claim", "Medium", "needs_human_verification", lambda m: "a trusted",
     "Ranking claims (e.g. \"#1\") need a named, dated source. No approved fact supports it.",
     "Cite the ranking source (add it to Approved Facts) or use neutral wording."),
    ("promo-weak-superlative",
     re.compile(r"\b(?:best[- ]in[- ]class|unmatched|unrivall?ed|unbeatable|unparalleled|second to none|"
                r"revolutionary|game[- ]chang(?:ing|er)|cutting[- ]edge|best[- ]selling)\b", re.I),
     "Exaggerated promotional wording", "Low", "needs_human_verification", _repl_weak,
     "Promotional superlatives can read as unsupported claims and may need evidence or approval.",
     "Use specific, factual descriptions instead."),
    ("promo-instant-outcome",
     re.compile(r"\b(?:become|get|be|grow|turn)\s+(?:successful|rich|wealthy|famous|fit|slim|profitable)"
                r"\s+(?:instantly|overnight|immediately|quickly|fast)\b", re.I),
     "Unrealistic outcome promise", "High", "none", _repl_instant_phrase,
     "Promising personal or business success instantly sets an expectation that cannot be substantiated.",
     "Describe what the product helps with rather than promising an immediate result."),
    ("promo-instant-word",
     re.compile(r"\b(?:instantly|overnight|effortlessly)\b", re.I),
     "Exaggerated speed or ease claim", "Medium", "needs_human_verification", _repl_instant_word,
     "Claims of instant or effortless results require supporting evidence or approval.",
     "Use measured wording or provide approved evidence."),
    ("promo-risk-free",
     re.compile(r"\b(?:risk[- ]free|zero[- ]risk|no[- ]risk|100\s?%\s+safe)\b", re.I),
     "Absolute safety / risk claim", "High", "needs_human_verification", _repl_risk,
     "Absolute claims of safety or zero risk generally need formal substantiation or legal approval.",
     "Remove the absolute claim or obtain approval."),
    ("promo-proven",
     re.compile(r"\b(?:scientifically|clinically|independently)?\s?proven(?:\s+results?)?\b", re.I),
     "\"Proven\" claim without evidence", "Medium", "needs_human_verification", None,
     "\"Proven\" implies independent evidence. None is recorded in your approved facts.",
     "Cite the study or source (add it to Approved Facts) or remove the word."),
    ("promo-absolutes",
     re.compile(r"\b(?:never fails?|always works?|works every time|everyone loves|all (?:our )?customers (?:love|are))\b", re.I),
     "Absolute generalisation", "Medium", "needs_human_verification", None,
     "Absolute generalisations about product performance or customer sentiment require evidence.",
     "Use qualified wording such as \"many customers\" backed by an approved source."),
    ("promo-regulated",
     re.compile(r"\b(?:cures?|get rich quick|double your money|guaranteed returns?)\b", re.I),
     "Potentially regulated claim", "High", "needs_human_verification", None,
     "Medical, financial-return or similar claims are typically regulated and need formal approval.",
     "Remove the claim or route it through legal/compliance review."),
]


def _supported_by_facts(rx, profile: BrandProfile) -> bool:
    return any(rx.search(stmt) for stmt, _ in profile.facts)


# --------------------------------------------------------------------------- checks
class Checker:
    def __init__(self, text: str, profile: BrandProfile, content_type: str = "website"):
        self.text = text or ""
        self.p = profile
        self.ctype = content_type
        self.findings: list[Finding] = []
        self.spans: list[tuple] = []

    def add(self, f: Finding) -> bool:
        if f.span and _overlaps(f.span, self.spans):
            return False
        if f.span:
            self.spans.append(f.span)
        self.findings.append(f)
        return True

    # --- B. terminology
    def terminology(self):
        p, text = self.p, self.text
        terms = [(pref, list(vs)) for pref, vs in p.approved_terms]
        if p.company_name and not any(pref.lower() == p.company_name.lower() for pref, _ in terms):
            terms.insert(0, (p.company_name, []))
        for pref, variants in terms:
            auto = list(variants)
            words = pref.split()
            if len(words) >= 2:  # abbreviated company/product names, e.g. "ABC Tech" for "ABC Technologies"
                head, last = " ".join(words[:-1]), words[-1]
                for k in range(len(last) - 1, 2, -1):
                    auto.append(f"{head} {last[:k]}")
            for var in sorted(set(auto), key=len, reverse=True):
                rx = re.compile(r"(?<![\w])" + re.escape(var).replace(r"\ ", r"\s+") + r"(?![\w])", re.I if var.lower() != pref.lower() else 0)
                for m in rx.finditer(text):
                    if m.group(0) == pref:
                        continue
                    # skip when this is part of the correct full name, e.g. "ABC Tech" inside "ABC Technologies"
                    if text[m.start(): m.start() + len(pref)].lower() == pref.lower():
                        continue
                    f = Finding(
                        title="Incorrect company / product name" if pref == p.company_name else "Non-approved terminology",
                        category="Terminology Consistency", severity="High" if pref == p.company_name else "Medium",
                        original_text=m.group(0),
                        explanation=f"\"{m.group(0)}\" is not the approved name. The approved term is \"{pref}\". "
                                    "Inconsistent naming weakens brand recognition.",
                        guideline=f"Approved terminology: use \"{pref}\".",
                        suggestion=f"Replace with \"{pref}\".", suggested_text=pref,
                        related_source="Brand guidelines – approved terminology", rule_id="term-variant",
                        span=(m.start(), m.end()))
                    self.add(f)
            # casing check on the exact approved term
            for m in re.finditer(r"(?<![\w])" + re.escape(pref).replace(r"\ ", r"\s+") + r"(?![\w])", text, re.I):
                if m.group(0) != pref and not _overlaps((m.start(), m.end()), self.spans):
                    self.add(Finding(
                        title="Inconsistent capitalisation of approved term", category="Terminology Consistency",
                        severity="Low", original_text=m.group(0),
                        explanation=f"The approved spelling is \"{pref}\".",
                        guideline=f"Approved terminology: use \"{pref}\".", suggestion=f"Use \"{pref}\".",
                        suggested_text=pref, related_source="Brand guidelines – approved terminology",
                        rule_id="term-case", span=(m.start(), m.end())))

    def prohibited(self):
        for word, repl in self.p.prohibited:
            rx = re.compile(r"(?<![\w])" + re.escape(word) + r"(?![\w])", re.I)
            for m in rx.finditer(self.text):
                suggested = match_case(m.group(0), repl) if repl else None
                self.add(Finding(
                    title="Prohibited word or phrase", category="Terminology Consistency", severity="High",
                    original_text=m.group(0),
                    explanation=f"\"{word}\" is on your organisation's restricted word list.",
                    guideline=f"Restricted word list: avoid \"{word}\".",
                    suggestion=(f"Use \"{repl}\" instead." if repl else "Remove or rephrase this wording."),
                    suggested_text=suggested if repl else None,
                    related_source="Brand guidelines – restricted words", rule_id="prohibited-word",
                    span=(m.start(), m.end())))

    # --- D. promotional claims
    def listed_claims(self):
        for phrase, repl in self.p.claims:
            rx = re.compile(r"(?<![\w])" + re.escape(phrase).replace(r"\ ", r"\s+") + r"(?![\w])", re.I)
            for m in rx.finditer(self.text):
                self.add(Finding(
                    title="Unsupported claim listed in guidelines", category="Promotional Claims", severity="High",
                    original_text=m.group(0),
                    explanation=f"Your guidelines list \"{phrase}\" as an unsupported claim to avoid. No approved "
                                "evidence is recorded that would allow it.",
                    guideline=f"Unsupported claims to avoid: \"{phrase}\".",
                    suggestion=(f"Rephrase as \"{repl}\"." if repl else "Remove the claim or obtain approval."),
                    suggested_text=match_case(m.group(0), repl) if repl else None,
                    related_source="Brand guidelines – unsupported claims", rule_id="claim-listed",
                    span=(m.start(), m.end())))

    def promotional(self):
        for rid, rx, title, sev, ver, repl, expl, advice in PROMO_RULES:
            if _supported_by_facts(rx, self.p):
                continue  # approved evidence exists in the guidelines
            for m in rx.finditer(self.text):
                if not m.group(0).strip():
                    continue
                orig = m.group(0).strip()
                start = m.start() + (len(m.group(0)) - len(m.group(0).lstrip()))
                r = repl(m) if repl else None
                self.add(Finding(
                    title=title, category="Promotional Claims", severity=sev, original_text=orig,
                    explanation=expl, guideline="Promotional claims must be supported by approved evidence or approval.",
                    suggestion=advice, suggested_text=match_case(orig, r) if r else None,
                    related_source="No approved supporting fact found" if ver != "none" else "",
                    verification=ver, rule_id=rid, span=(start, start + len(orig))))

    # --- C. facts
    CLAIM_RE = re.compile(
        r"(\b\d[\d,\.]*\s?(?:%|\+|k\b|m\b|million|billion|years?|customers?|clients?|users?|countries|offices|"
        r"employees|awards?|partners)|\b(?:since|founded|established)\s+(?:in\s+)?\d{4}|\b(?:ISO|SOC)\s?\d+|"
        r"award[- ]winning|\bcertified\b|\branked\b|\btrusted by\b)", re.I)
    NUM_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")

    @staticmethod
    def _nums(s: str) -> list:
        out = []
        for n in Checker.NUM_RE.findall(s):
            out.append(n.replace(",", "").rstrip("."))
        return out

    def facts(self):
        facts = self.p.facts
        own = set()
        for w in re.findall(r"[A-Za-z]+", self.p.company_name):
            own.add(w.lower())
            own.add(_stem(w.lower()))
        for sent, s0, s1 in sentences(self.text):
            if not self.CLAIM_RE.search(sent):
                continue
            # a promotional finding with a number inside this sentence (e.g. "100% guaranteed") already covers it
            if any(f.category == "Promotional Claims" and f.span and s0 <= f.span[0] < s1 and re.search(r"\d", f.original_text)
                   for f in self.findings):
                continue
            st = set(tokens(sent, own))
            best, best_score = None, 0.0
            for stmt, src in facts:
                ft = set(tokens(stmt, own))
                common = st & ft
                if not common:
                    continue
                score = len(common) / max(1, min(len(st), len(ft)))
                if (len(common) >= 2 or score == 1.0) and score > best_score:
                    best, best_score = (stmt, src), score
            sn = self._nums(sent)
            if best and best_score >= 0.5:
                stmt, src = best
                fn = self._nums(stmt)
                if sn and fn and not set(sn) <= set(fn):
                    repl = None
                    if len(sn) == 1 and len(fn) == 1:
                        for tok in re.findall(r"\d[\d,]*(?:\.\d+)?", sent):
                            if tok.replace(",", "") == sn[0]:
                                repl = sent.replace(tok, fn[0], 1)
                                break
                    self.add(Finding(
                        title="Statement differs from an approved fact", category="Fact Verification",
                        severity="High", original_text=sent,
                        explanation=f"This statement contains figures ({', '.join(sn)}) that differ from the approved "
                                    f"fact \"{stmt}\" ({', '.join(fn)}). It conflicts with the approved source; the "
                                    "source itself may be out of date, so please confirm which is correct.",
                        guideline="Statements must match approved company facts.",
                        suggestion="Align the statement with the approved fact, or update the approved fact if it is outdated.",
                        suggested_text=repl, related_source=f"{stmt}" + (f" — Source: {src}" if src else ""),
                        verification="conflicts_with_approved_fact", rule_id="fact-conflict", pos=s0))
                continue  # figures agree (or nothing to compare): supported by an approved fact
            self.add(Finding(
                title="Unverified factual claim", category="Fact Verification",
                severity="Medium" if sn else "Low", original_text=sent,
                explanation="This statement makes a factual claim (figures, certification, ranking or history) that "
                            "could not be matched to any approved fact. It is NOT necessarily wrong – it simply "
                            "cannot be verified automatically.",
                guideline="Factual claims should be traceable to an approved source.",
                suggestion="Needs Human Verification: confirm against a source and add it to Approved Facts, or remove the claim.",
                suggested_text=None, related_source="No matching approved fact found",
                verification="needs_human_verification", rule_id="fact-unverified", pos=s0))

    # --- A. tone
    def tone(self):
        style = self.p.tone_style
        if style == "casual":
            return
        text = self.text
        for word, repl in sorted(SLANG.items(), key=lambda kv: -len(kv[0])):
            for m in re.finditer(r"(?<![\w])" + re.escape(word) + r"(?![\w])", text, re.I):
                self.add(Finding(
                    title="Informal wording", category="Tone Consistency", severity="Medium",
                    original_text=m.group(0),
                    explanation=f"\"{m.group(0)}\" is informal and does not match the approved "
                                f"{style.replace('friendly', 'professional and friendly')} voice.",
                    guideline=f"Brand voice: {self.p.voice or style}",
                    suggestion=(f"Use \"{repl}\"." if repl else "Remove this informal expression."),
                    suggested_text=match_case(m.group(0), repl) if repl else "",
                    related_source="Brand voice and tone", rule_id="tone-slang", span=(m.start(), m.end())))
        for phrase, repl in PRESSURE.items():
            for m in re.finditer(r"(?<![\w])" + re.escape(phrase).replace("'", "['’]?") + r"(?![\w])", text, re.I):
                self.add(Finding(
                    title="Pushy call-to-action", category="Tone Consistency", severity="Low",
                    original_text=m.group(0),
                    explanation="High-pressure sales language conflicts with a professional, trustworthy voice.",
                    guideline=f"Brand voice: {self.p.voice or style}",
                    suggestion=f"Use a softer call-to-action such as \"{repl}\".",
                    suggested_text=match_case(m.group(0), repl), related_source="Brand voice and tone",
                    rule_id="tone-pressure", span=(m.start(), m.end())))
        if style == "formal":
            n = 0
            for m in re.finditer(r"\b\w+['’]\w+\b", text):
                key = m.group(0).lower().replace("’", "'")
                if key in CONTRACTION_MAP and n < 5:
                    n += 1
                    self.add(Finding(
                        title="Contraction in formal copy", category="Tone Consistency", severity="Low",
                        original_text=m.group(0),
                        explanation="The approved voice is formal; contractions read as conversational.",
                        guideline=f"Brand voice: {self.p.voice or 'Formal'}",
                        suggestion=f"Write \"{CONTRACTION_MAP[key]}\".",
                        suggested_text=match_case(m.group(0), CONTRACTION_MAP[key]),
                        related_source="Brand voice and tone", rule_id="tone-contraction", span=(m.start(), m.end())))
        limit = 1 if style in ("formal", "professional") else 2
        extra = 0
        seen = 0
        for sent, s0, s1 in sentences(text):
            if sent.endswith("!"):
                seen += 1
                if seen > limit and extra < 5:
                    extra += 1
                    self.add(Finding(
                        title="Excessive exclamation marks", category="Tone Consistency", severity="Low",
                        original_text=sent,
                        explanation="Repeated exclamation marks sound overly excited for the approved brand voice.",
                        guideline=f"Brand voice: {self.p.voice or style}",
                        suggestion="End the sentence with a full stop.",
                        suggested_text=re.sub(r"!+$", ".", sent), related_source="Brand voice and tone",
                        rule_id="tone-exclaim", pos=s0))
        allowed_caps = {w.upper() for w in self.p.company_name.split()} | ACRONYM_WHITELIST
        for pref, vs in self.p.approved_terms:
            allowed_caps |= {w.upper() for w in pref.split()}
        for m in re.finditer(r"\b[A-Z]{3,}(?:\s+[A-Z]{3,})+\b|\b[A-Z]{4,}\b", text):
            words = m.group(0).split()
            if all(w in allowed_caps for w in words):
                continue
            if len(words) == 1 and words[0] in allowed_caps:
                continue
            self.add(Finding(
                title="ALL-CAPS text", category="Tone Consistency", severity="Low", original_text=m.group(0),
                explanation="Capitalised words read as shouting and are harder to read.",
                guideline=f"Brand voice: {self.p.voice or style}", suggestion="Use sentence case.",
                suggested_text=m.group(0).capitalize(),
                related_source="Brand voice and tone", rule_id="tone-caps", span=(m.start(), m.end())))
        emoji_limit = 0 if style == "formal" else 2
        emojis = EMOJI_RE.findall(text)
        if len(emojis) > emoji_limit:
            first = EMOJI_RE.search(text)
            self.add(Finding(
                title="Emoji use outside brand voice", category="Tone Consistency", severity="Low",
                original_text=first.group(0),
                explanation=f"{len(emojis)} emoji found; the approved {style} voice allows at most {emoji_limit}.",
                guideline=f"Brand voice: {self.p.voice or style}", suggestion="Remove some or all emoji.",
                suggested_text="", related_source="Brand voice and tone", rule_id="tone-emoji",
                span=(first.start(), first.end())))

    # --- slogans (fuzzy)
    def slogans(self):
        for slogan in self.p.slogans:
            for sent, s0, s1 in sentences(self.text):
                if re.search(re.escape(slogan) + r"(?![\w])", sent):
                    continue  # exact approved slogan
                ratio = difflib.SequenceMatcher(None, slogan.lower(), sent.lower().strip(".!? ")).ratio()
                if 0.72 <= ratio < 1.0:
                    self.add(Finding(
                        title="Approved slogan altered", category="Terminology Consistency", severity="Medium",
                        original_text=sent,
                        explanation=f"This sentence closely resembles the approved slogan \"{slogan}\" but is worded differently.",
                        guideline=f"Approved slogan: \"{slogan}\"", suggestion="Use the exact approved slogan.",
                        suggested_text=slogan, related_source="Brand guidelines – approved slogans",
                        rule_id="slogan-variant", pos=s0))

    # --- F. accessibility (text/HTML level)
    def accessibility(self):
        text = self.text
        for m in re.finditer(r"<img\b[^>]*>", text, re.I):
            tag = m.group(0)
            alt = re.search(r"\balt\s*=\s*(\"([^\"]*)\"|'([^']*)')", tag, re.I)
            if not alt or not (alt.group(2) or alt.group(3) or "").strip():
                self.add(Finding(
                    title="Missing alternative text", category="Accessibility", severity="Medium", original_text=tag,
                    explanation="Images need descriptive alternative text so screen-reader users can understand them.",
                    guideline="Accessibility: all informative images require alt text.",
                    suggestion="Add a concise description with the alt attribute (use alt=\"\" only for purely decorative images).",
                    suggested_text=None, related_source="WCAG 2.1 – 1.1.1 Non-text Content",
                    rule_id="a11y-img-alt", span=(m.start(), m.end())))
        for m in re.finditer(r"(?<![\w])(click here|read more|here)(?![\w])", text, re.I):
            if m.group(1).lower() == "here" and not re.search(r"<a\b[^>]*>\s*here\s*</a>", text[max(0, m.start() - 80): m.end() + 6], re.I):
                continue
            self.add(Finding(
                title="Non-descriptive link text", category="Accessibility", severity="Low", original_text=m.group(0),
                explanation="Link text such as \"click here\" is meaningless out of context for screen-reader users.",
                guideline="Accessibility: link text should describe its destination.",
                suggestion="Use descriptive link text such as \"view our pricing plans\".", suggested_text=None,
                related_source="WCAG 2.1 – 2.4.4 Link Purpose", rule_id="a11y-link", span=(m.start(), m.end())))
        if self.ctype == "social":
            for m in re.finditer(r"#[a-z]{14,}\b", text):
                self.add(Finding(
                    title="Hashtag not in CamelCase", category="Accessibility", severity="Low", original_text=m.group(0),
                    explanation="Long all-lowercase hashtags are read letter by letter by many screen readers.",
                    guideline="Accessibility: use #CamelCase hashtags.", suggestion="Capitalise each word in the hashtag.",
                    suggested_text=None, related_source="Social accessibility best practice",
                    rule_id="a11y-hashtag", span=(m.start(), m.end())))
        words = WORD_RE.findall(text)
        if len(words) >= 40:
            fre = _flesch(text)
            if fre < 30:
                self.add(Finding(
                    title="Text may be difficult to read", category="Accessibility", severity="Low",
                    original_text=text[:80].strip() + ("…" if len(text) > 80 else ""),
                    explanation=f"Estimated Flesch reading ease is {fre:.0f} (very difficult). Long sentences and complex "
                                "words reduce readability for many audiences.",
                    guideline="Accessibility: aim for clear, plain language.",
                    suggestion="Shorten sentences and prefer simpler words.", suggested_text=None,
                    related_source="Plain-language guidance", rule_id="a11y-readability"))
        for sent, s0, s1 in sentences(text):
            if len(WORD_RE.findall(sent)) > 45:
                self.add(Finding(
                    title="Very long sentence", category="Accessibility", severity="Low",
                    original_text=sent[:160] + ("…" if len(sent) > 160 else ""),
                    explanation="Sentences of more than 45 words are hard to follow.",
                    guideline="Accessibility: aim for clear, plain language.", suggestion="Split into shorter sentences.",
                    suggested_text=None, related_source="Plain-language guidance", rule_id="a11y-long-sentence"))
                break

    def run(self) -> list:
        self.terminology()
        self.prohibited()
        self.listed_claims()
        self.promotional()
        self.facts()
        self.tone()
        self.slogans()
        self.accessibility()
        self.findings.sort(key=lambda f: (f.span[0] if f.span else f.pos))
        return self.findings


def check_text(text: str, profile: BrandProfile, content_type: str = "website") -> list:
    return Checker(text, profile, content_type).run()


# --------------------------------------------------------------------------- scoring
def compute_score(findings) -> dict:
    """findings: iterable with .severity and .verification attributes."""
    deduction = 0
    counts = {"High": 0, "Medium": 0, "Low": 0}
    for f in findings:
        w = SEV_WEIGHT.get(f.severity, 0)
        counts[f.severity] = counts.get(f.severity, 0) + 1
        if f.verification == "needs_human_verification":
            w = (w + 1) // 2
        deduction += w
    score = max(0, 100 - deduction)
    total = sum(counts.values())
    if total == 0:
        label = "Ready for human review"
    elif score >= 85 and counts["High"] == 0:
        label = "Ready for human review (minor issues)"
    elif score >= 60:
        label = "Revisions recommended before review"
    else:
        label = "Significant revisions needed"
    return {"score": score, "label": label, "counts": counts, "total": total, "deduction": deduction}

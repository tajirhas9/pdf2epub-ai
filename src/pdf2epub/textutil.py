"""Script detection, word validity and text-layer quality scoring (language-agnostic)."""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from functools import lru_cache

from pdf2epub.ir import Word

# Unicode ranges -> script name used by OCR model selection.
_SCRIPT_RANGES: list[tuple[int, int, str]] = [
    (0x0590, 0x05FF, "hebrew"),
    (0x0600, 0x06FF, "arabic"),
    (0x0750, 0x077F, "arabic"),
    (0x08A0, 0x08FF, "arabic"),
    (0xFB50, 0xFDFF, "arabic"),
    (0xFE70, 0xFEFF, "arabic"),
    (0x0400, 0x04FF, "cyrillic"),
    (0x0370, 0x03FF, "greek"),
    (0x0900, 0x097F, "devanagari"),
    (0x0E00, 0x0E7F, "thai"),
    (0x0B80, 0x0BFF, "tamil"),
    (0x0C00, 0x0C7F, "telugu"),
    (0xAC00, 0xD7AF, "korean"),
    (0x3040, 0x30FF, "cjk"),
    (0x4E00, 0x9FFF, "cjk"),
]
RTL_SCRIPTS = {"arabic", "hebrew"}

# Footnote label at the start of a note ("1 See…", "²) …"). Not a reference that starts a
# continued note ("1/69). We gave…", "2:185"): those digits are followed by / : , or a dash.
NOTE_LABEL_RE = re.compile(r"^\s*(\d{1,3}|[⁰¹²³⁴⁵⁶⁷⁸⁹]{1,3})(?![\d⁰¹²³⁴⁵⁶⁷⁸⁹/:,\-–])[.)]?\s*")
UNSUP = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹", "0123456789")
# Default BCP-47 tag per script when no language hint matches.
SCRIPT_LANG = {
    "latin": "en",
    "arabic": "ar",
    "hebrew": "he",
    "cyrillic": "ru",
    "greek": "el",
    "devanagari": "hi",
    "thai": "th",
    "tamil": "ta",
    "telugu": "te",
    "korean": "ko",
    "cjk": "zh",
}
LANG_SCRIPT = {
    "ar": "arabic",
    "fa": "arabic",
    "ur": "arabic",
    "ps": "arabic",
    "ug": "arabic",
    "sd": "arabic",
    "he": "hebrew",
    "yi": "hebrew",
    "ru": "cyrillic",
    "uk": "cyrillic",
    "bg": "cyrillic",
    "sr": "cyrillic",
    "el": "greek",
    "hi": "devanagari",
    "mr": "devanagari",
    "ne": "devanagari",
    "th": "thai",
    "ta": "tamil",
    "te": "telugu",
    "ko": "korean",
    "zh": "cjk",
    "ja": "cjk",
}


def char_script(ch: str) -> str | None:
    cp = ord(ch)
    for lo, hi, name in _SCRIPT_RANGES:
        if lo <= cp <= hi:
            return name
    if ch.isalpha():
        return "latin"
    return None


def dominant_script(text: str) -> str | None:
    counts = Counter(s for ch in text if (s := char_script(ch)))
    return counts.most_common(1)[0][0] if counts else None


def is_pua(ch: str) -> bool:
    cp = ord(ch)
    return 0xE000 <= cp <= 0xF8FF or 0xF0000 <= cp <= 0xFFFFD or ch == "�"


def lang_for_script(script: str, hints: list[str]) -> str:
    for h in hints:
        if LANG_SCRIPT.get(h, "latin") == script:
            return h
    return SCRIPT_LANG.get(script, "und")


def direction(script: str | None) -> str | None:
    return "rtl" if script in RTL_SCRIPTS else None


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFC", text)
    return re.sub(r"[ \t\u00a0]+", " ", text)


def fix_punct_spacing(text: str) -> str:
    """'history,this' -> 'history, this'; 'water.Ever' -> 'water. Ever' (OCR lost the space).
    Leaves numbers (1,000 / 3.5), abbreviations (e.g.) and URLs alone."""
    text = re.sub(r"(?<=[^\W\d_]{2})([,;])(?=[^\W\d_]{2})", r"\1 ", text)
    return re.sub(r"(?<=[a-z]{2})([.!?])(?=[A-Z][a-z])", r"\1 ", text)


# ---- word validity ------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _wordfreq():
    try:
        import wordfreq

        return wordfreq
    except Exception:  # optional dependency
        return None


_WORD_RE = re.compile(r"[^\W\d_]+(?:['’][^\W\d_]+)?", re.U)


@lru_cache(maxsize=200_000)
def is_known(word: str, lang: str) -> bool | None:
    """True/False if a frequency list exists for `lang`, None when we cannot tell."""
    wf = _wordfreq()
    if wf is None:
        return None
    try:
        return wf.zipf_frequency(word.lower(), lang) > 1.0
    except Exception:
        return None


def known_ratio(tokens: list[str], lang: str) -> float | None:
    words = [t for t in tokens if len(t) >= 2]
    if not words:
        return None
    verdicts = [is_known(w, lang) for w in words]
    if any(v is None for v in verdicts):
        return None
    return sum(bool(v) for v in verdicts) / len(verdicts)


def text_layer_quality(words: list[Word], lang: str = "en") -> float | None:
    """0..1 estimate of how trustworthy a PDF text layer is. None = no usable text layer.

    Signals: split words (many 1-letter tokens), junk tokens (no letters/digits, PUA, U+FFFD),
    dictionary hit-rate for the document language when a frequency list is available.
    """
    tokens = [w.text for w in words if w.text.strip()]
    if len(tokens) < 8:
        return None
    alpha = [m.group(0) for t in tokens for m in _WORD_RE.finditer(t)]
    if not alpha:
        return 0.0
    single = sum(1 for t in alpha if len(t) == 1 and char_script(t) == "latin" and t not in "aAI") / len(alpha)
    junk = sum(1 for t in tokens if not any(c.isalnum() for c in t) and len(t) > 1 or any(is_pua(c) for c in t))
    junk /= len(tokens)
    score = (1 - min(1.0, single * 3)) * (1 - min(1.0, junk * 3))
    kr = known_ratio(alpha, lang)
    if kr is not None:
        score *= min(1.0, kr / 0.9)
    return round(max(0.0, min(1.0, score)), 3)


def join_lines(lines: list[str]) -> str:
    """Join text lines into a paragraph, undoing end-of-line hyphenation."""
    out = ""
    for line in (normalize(line).strip() for line in lines):
        if not line:
            continue
        if not out:
            out = line
        elif re.search(r"[^\W\d_]-$", out) and line[:1].islower():
            out = out[:-1] + line
        else:
            out += " " + line
    return out

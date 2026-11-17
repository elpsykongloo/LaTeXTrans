"""Conservative checks for untranslated prose, excluding TeX syntax and code."""

import re

from .utils import LATEX_PROTECTED_TOKEN_PATTERN, protect_latex_syntax, target_language_family


def visible_prose(text: str) -> str:
    protected = protect_latex_syntax(text).protected_text
    return LATEX_PROTECTED_TOKEN_PATTERN.sub(" ", protected)


def untranslated_prose(source: str, translated: str, source_language: str, target_language: str):
    """Find whole English passages retained in Chinese/Japanese translations.

    Names, short technical labels, formulas and code are intentionally excluded.
    This detects omissions; it is not a score for semantic translation quality.
    """
    if source_language.lower() not in {"en", "english"} or target_language_family(target_language) not in {"ch", "ja"}:
        return []
    source_words = " ".join(re.findall(r"[A-Za-z]+", visible_prose(source))).lower()
    findings = []
    for paragraph in re.split(r"\n\s*\n", visible_prose(translated)):
        # Bilingual quotations and retained names in translated paragraphs are OK.
        if re.search(r"[\u3040-\u30ff\u3400-\u9fff]", paragraph):
            continue
        words = re.findall(r"[A-Za-z]+", paragraph)
        if len(words) < 16:
            continue
        lower = [word.lower() for word in words]
        if sum(word in {"the", "a", "an", "of", "in", "to", "is", "and", "that", "we"} for word in lower) < 3:
            continue
        for start in range(len(words) - 15):
            if " ".join(lower[start:start + 16]) in source_words:
                findings.append(" ".join(words[start:start + 24]))
                break
    return findings

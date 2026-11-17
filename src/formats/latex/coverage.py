"""Conservative checks for untranslated prose, excluding TeX syntax and code."""

import re

from .utils import LATEX_PROTECTED_TOKEN_PATTERN, get_command_pattern, mask_latex_opaque_content, protect_latex_syntax, target_language_family


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
    candidates = [(paragraph, 16, 3) for paragraph in re.split(r"\n\s*\n", visible_prose(translated))]
    # A translated paragraph can contain an untouched short explanatory note.
    # Inspect note bodies separately; URLs and code still remain opaque.
    for note in get_command_pattern("footnote|footnotetext|thanks").finditer(mask_latex_opaque_content(translated)):
        candidates.append((visible_prose(note.group(4)), 8, 2))
    for paragraph, minimum_words, minimum_function_words in candidates:
        # Bilingual quotations and retained names in translated paragraphs are OK.
        if re.search(r"[\u3040-\u30ff\u3400-\u9fff]", paragraph):
            continue
        words = re.findall(r"[A-Za-z]+", paragraph)
        if len(words) < minimum_words:
            continue
        lower = [word.lower() for word in words]
        if sum(word in {"the", "a", "an", "of", "in", "to", "is", "and", "that", "we", "are", "at", "for", "this"} for word in lower) < minimum_function_words:
            continue
        for start in range(len(words) - minimum_words + 1):
            if " ".join(lower[start:start + minimum_words]) in source_words:
                excerpt = " ".join(words[start:start + 24])
                if excerpt not in findings:
                    findings.append(excerpt)
                break
    return findings

"""Adjust manually positioned wrapping floats after translated text reflows."""

import re
from pathlib import Path

from .utils import get_command_pattern, get_latex_macro_definitions, get_latex_opaque_fragments


def reflow_wrapping_floats(tex: str) -> str:
    """Let wrapfig defer figures until they fit, preserving their side and width.

    Manual line counts and negative vertical skips were tuned to the source
    language. Reusing them after translation can put an image over footnotes
    or carry a narrow text column across a page boundary.
    """
    protected = [(start, end) for start, end, _ in get_latex_opaque_fragments(tex)]
    protected.extend((start, end) for start, end, _ in get_latex_macro_definitions(tex))
    pattern = re.compile(
        r"\\begin\s*\{wrap(?P<kind>figure|table)\}\s*"
        r"(?:\[\s*\d+\s*\]\s*)?\{(?P<side>[rRlLiIoO])\}"
        r"(?P<body>.*?)\\end\s*\{wrap(?P=kind)\}", re.DOTALL,
    )

    def replace(match):
        if any(start <= match.start() < end for start, end in protected):
            return match.group(0)
        body = re.sub(r"\\vspace\*?\s*\{\s*-\s*\d*\.?\d+\s*(?:mm|cm|pt|in|ex|em)\s*\}", "", match.group("body"))
        kind = match.group("kind")
        return (r"\begin{wrap" + kind + "}{" + match.group("side").upper() + "}"
                + body + r"\end{wrap" + kind + "}")

    result = pattern.sub(replace, tex)
    # Delay an immediately preceding heading until the measured wrap has room.
    # Otherwise a page break at endwrapfigure can orphan that heading.
    headings = []
    protected = [(start, end) for start, end, _ in get_latex_opaque_fragments(result)]
    protected.extend((start, end) for start, end, _ in get_latex_macro_definitions(result))
    for heading in get_command_pattern(r"section|subsection|subsubsection").finditer(result):
        following = re.match(r"(?:\s|\\label\s*\{[^{}]*\})*(?=\\begin\s*\{wrap(?:figure|table)\})", result[heading.end():])
        if following and not any(start <= heading.start() < end for start, end in protected):
            end = heading.end() + following.end()
            headings.append((heading.start(), end))
    for start, end in reversed(headings):
        result = result[:start] + r"\latextranswrapheading{" + result[start:end] + "}\n" + result[end:]
    return result


def add_cjk_layout_support(tex: str) -> str:
    """Reserve a wrap's measured height and support the word-based contour underline.

    The underline adapter is guarded by the implementation's helper names; an
    unrelated user macro named fancyul is left alone.
    """
    marker = "% LaTeXTrans: translated layout support"
    if marker in tex:
        return tex
    lines = []
    if re.search(r"\\begin\s*\{wrap(?:figure|table)\}", tex):
        # wrapfig computes WF@size from the image + translated caption, before
        # starting its everypar handler. Reserve that actual height here so a
        # deferred wrap cannot leak into a later section or overlap footnotes.
        lines.extend([
            r"\makeatletter",
            r"\let\latextranspendingheading\@empty",
            r"\newcommand{\latextranswrapheading}[1]{\gdef\latextranspendingheading{#1}}",
            r"\newcommand{\latextransfinishwrap}{%",
            r"  \ifnum\c@WF@wrappedlines>\@ne\par",
            r"  \ifnum\c@WF@wrappedlines>\@ne",
            r"    \@tempdima\baselineskip\multiply\@tempdima\c@WF@wrappedlines",
            r"    \advance\@tempdima-\baselineskip\vskip\@tempdima",
            r"    \global\c@WF@wrappedlines\z@\WF@finale\fi\fi}",
            r"\AddToHook{cmd/WF@startfloating/before}{%",
            r"  \ifinner\else\par",
            r"    \@tempdima\WF@size",
            r"    \ifx\latextranspendingheading\@empty\else\advance\@tempdima4\baselineskip\fi",
            r"    \ifdim\dimexpr\pagegoal-\pagetotal\relax<\@tempdima\newpage\fi",
            r"  \fi\latextranspendingheading\global\let\latextranspendingheading\@empty",
            r"  \global\WF@floatfalse}",
            r"\AddToHook{cmd/section/before}{\latextransfinishwrap}",
            r"\AddToHook{cmd/subsection/before}{\latextransfinishwrap}",
            r"\AddToHook{cmd/subsubsection/before}{\latextransfinishwrap}",
            r"\makeatother",
        ])
        environments = set(re.findall(r"\\begin\s*\{([A-Za-z]+)\}", tex))
        for env in sorted(environments):
            if env.lower().endswith("box") or env in {"mdframed", "itemize", "enumerate", "description"}:
                lines.append(r"\AddToHook{env/" + env + r"/before}{\latextransfinishwrap}")
    if re.search(r"\\fancyul(?:\[|\{|\s)", tex):
        lines.extend([
            r"\usepackage{xeCJKfntef}",
            r"\AtBeginDocument{\ifcsname fancyul_word:n\endcsname",
            r"  \ifcsname fancyul_space:\endcsname",
            r"    \RenewDocumentCommand{\fancyul}{O{primary} O{white} m}{%",
            r"      \CJKunderline[format=\color{#1}]{#3}}%",
            r"  \fi\fi}",
        ])
    if not lines:
        return tex
    return re.sub(r"\\begin\s*\{document\}",
                  lambda match: marker + "\n" + "\n".join(lines) + "\n" + match.group(0), tex, count=1)


def inspect_layout_log(log_path) -> dict:
    """Expose severe TeX layout warnings separately from compilation success."""
    path = Path(log_path)
    try:
        log = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {"status": "unavailable", "log_path": str(path), "warnings": []}
    warnings = []
    for match in re.finditer(r"Overfull \\[hv]box \(([\d.]+)pt [^\n]+", log):
        if float(match.group(1)) >= 10:
            warnings.append(match.group(0).strip())
    warnings.extend(match.group(0).strip() for match in re.finditer(r"Package wrapfig Warning:[^\n]+", log))
    return {"status": "warnings" if warnings else "no_warnings", "log_path": str(path), "warnings": warnings}

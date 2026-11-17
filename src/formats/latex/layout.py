"""Adjust manually positioned wrapping floats after translated text reflows."""

import re
from pathlib import Path

from .utils import _find_balanced_group_end, get_command_pattern, get_latex_environment_tokens, get_latex_macro_definitions, get_latex_opaque_fragments


def suppress_build_date(tex: str) -> str:
    """Do not present today's rebuild date as the translated paper's date.

    Only the standard dynamic date is suppressed. Explicit dates and custom
    class date formats are retained; no publication date is guessed.
    """
    marker = "% LaTeXTrans: suppress dynamic rebuild date"
    if marker in tex or not re.search(r"\\maketitle\b", tex):
        return tex
    support = "\n".join([
        marker,
        r"\makeatletter",
        r"\AtBeginDocument{%",
        r"  \def\latextransdynamicdate{\today}%",
        r"  \ifx\@date\latextransdynamicdate\date{}\fi}",
        r"\makeatother",
    ])
    return re.sub(r"\\begin\s*\{document\}", lambda match: support + "\n" + match.group(0), tex, count=1)


def fit_floating_tables(tex: str) -> str:
    """Bound ordinary float tables to their current column width, never enlarge.

    Author tabulars, longtables and tabularx retain their own layout. Only the
    outermost tabular is boxed so nested cell tabulars cannot reuse the savebox.
    """
    cleaned, tokens = get_latex_environment_tokens(tex)
    protected = [(start, end) for start, end, _ in get_latex_macro_definitions(cleaned)]
    protected.extend((start, end) for start, end, _ in get_latex_opaque_fragments(cleaned))
    # Existing sizing wrappers own the dimensions and grouping of their content.
    # Boxing their tabular again can interfere with graphicx's saved box state.
    for command in re.finditer(r"\\(resizebox|scalebox|adjustbox)\*?(?![A-Za-z])", cleaned):
        index, groups = command.end(), 0
        required = 3 if command[1] == "resizebox" else 2
        while groups < required:
            whitespace = re.match(r"(?:\s|%[^\n]*(?:\n|$))*", cleaned[index:])
            index += whitespace.end()
            if index >= len(cleaned) or cleaned[index] not in "[{":
                break
            opening = cleaned[index]
            end = _find_balanced_group_end(cleaned, index, opening, "]" if opening == "[" else "}")
            if end is None:
                break
            groups += opening == "{"
            index = end
        if groups == required:
            protected.append((command.start(), index))
    stack, spans = [], []
    for token in tokens:
        if any(start <= token.start() < end for start, end in protected):
            continue
        name = token.group("name")
        if token.group("kind") == "begin":
            ancestors = {entry[0] for entry in stack}
            selected = (name in {"tabular", "tabular*"} and bool(ancestors & {"table", "table*"})
                        and not ancestors & {"tabular", "tabular*", "latextranstable", "adjustbox"})
            stack.append((name, token.start(), selected))
        elif stack and stack[-1][0] == name:
            _, start, selected = stack.pop()
            if selected:
                spans.append((start, token.end()))
    if not spans:
        return tex
    for start, end in reversed(sorted(spans)):
        cleaned = cleaned[:start] + r"\begin{latextranstable}" + cleaned[start:end] + r"\end{latextranstable}" + cleaned[end:]
    support = "\n".join([
        "% LaTeXTrans: fit floating tables to the available column",
        r"\usepackage{graphicx}",
        r"\newsavebox{\latextranstablebox}",
        r"\newenvironment{latextranstable}{%",
        r"  \edef\latextranstablewidth{\the\linewidth}%",
        r"  \begin{lrbox}{\latextranstablebox}%",
        r"}{%",
        r"  \end{lrbox}%",
        r"  \ifdim\wd\latextranstablebox>\latextranstablewidth\relax",
        r"    \typeout{LaTeXTrans: scaled table from \the\wd\latextranstablebox\space to \latextranstablewidth}%",
        r"    \resizebox{\latextranstablewidth}{!}{\usebox{\latextranstablebox}}%",
        r"  \else\usebox{\latextranstablebox}\fi}",
    ])
    return re.sub(r"\\begin\s*\{document\}", lambda match: support + "\n" + match.group(0), cleaned, count=1)


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
            if (env.lower().endswith("box") and env != "lrbox") or env in {"mdframed", "itemize", "enumerate", "description"}:
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
    adjustments = re.findall(r"LaTeXTrans: scaled table[^\n]+", log)
    return {"status": "warnings" if warnings else "no_warnings", "log_path": str(path), "warnings": warnings, "adjustments": adjustments}

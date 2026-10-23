"""
core/cleaning.py - FP-05: Tool-Ingestion Cleaning (HTML to Markdown & Bounded Python Summaries).

Architectural Purpose:
Reduces LLM token consumption and eliminates prompt injection vectors embedded in
boilerplate web structures (scripts, navigation, footers, hidden text) and large
code output blobs.

Features:
1. HTML to Clean Markdown Distillation:
   - Strips script, style, noscript, svg, nav, footer, header, aside, template, and hidden tags.
   - Detects hidden CSS styles ('display:none', 'visibility:hidden', 'aria-hidden="true"').
   - Converts headings to markdown headers (#, ##, ###) and list items to bullets (-).
   - Strips excessive repeated newlines and whitespace.
2. Bounded Execution Summarization:
   - Caps astronomical terminal output (e.g. 5,000 line print loops or huge arrays).
   - Extracts head + tail with explicit structural truncation markers.
   - CRITICAL: Never hides stderr or execution errors.
"""

import re
from html.parser import HTMLParser
from typing import List, Optional

_HIDDEN_STYLE_RE = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden", re.IGNORECASE)
_EXCESS_BLANK_LINES_RE = re.compile(r"\n{3,}")


class MarkdownHTMLDistiller(HTMLParser):
    """
    Parses HTML documents and extracts structured markdown text while eliminating
    navigation, headers, footers, scripts, and hidden adversarial elements.
    """

    _EXCLUDED_TAGS = {
        "script", "style", "noscript", "svg", "head", "nav",
        "footer", "header", "aside", "template", "form", "iframe",
    }

    def __init__(self) -> None:
        super().__init__()
        self._skip_depth = 0
        self._chunks: List[str] = []
        self._tag_stack: List[str] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        tag_lower = tag.lower()
        self._tag_stack.append(tag_lower)

        # Check for excluded tags
        if tag_lower in self._EXCLUDED_TAGS:
            self._skip_depth += 1
            return

        # Check for hidden CSS attributes
        attr_dict = {k.lower(): v for k, v in attrs if v is not None}
        style = attr_dict.get("style", "")
        if "hidden" in attr_dict or attr_dict.get("aria-hidden") == "true" or _HIDDEN_STYLE_RE.search(style):
            self._skip_depth += 1
            return

        # Markdown structural tags
        if self._skip_depth == 0:
            if tag_lower == "h1":
                self._chunks.append("\n\n# ")
            elif tag_lower == "h2":
                self._chunks.append("\n\n## ")
            elif tag_lower == "h3":
                self._chunks.append("\n\n### ")
            elif tag_lower in {"p", "div", "section", "article"}:
                self._chunks.append("\n\n")
            elif tag_lower in {"li"}:
                self._chunks.append("\n- ")
            elif tag_lower in {"br", "tr"}:
                self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag_lower = tag.lower()
        if tag_lower in self._tag_stack:
            self._tag_stack.pop()

        if tag_lower in self._EXCLUDED_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
        elif self._skip_depth > 0:
            # Check if we were skipping an element that just ended
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            cleaned = data.strip()
            if cleaned:
                self._chunks.append(cleaned + " ")

    def get_markdown(self) -> str:
        raw = "".join(self._chunks)
        # Normalize whitespace and blank lines
        lines = [line.strip() for line in raw.splitlines()]
        compact = "\n".join(line for line in lines if line)
        return _EXCESS_BLANK_LINES_RE.sub("\n\n", compact).strip()


def clean_html_to_markdown(html_content: str, max_chars: Optional[int] = None) -> str:
    """Convert HTML string to clean markdown, stripping scripts, navigation, and hidden text."""
    distiller = MarkdownHTMLDistiller()
    distiller.feed(html_content)
    text = distiller.get_markdown()
    if max_chars and len(text) > max_chars:
        return text[:max_chars] + f"\n\n... [Content truncated: {len(text) - max_chars} characters omitted]"
    return text


def summarize_code_output(
    stdout: str,
    stderr: str,
    max_lines: int = 40,
    max_chars: int = 2500,
) -> str:
    """
    Produce a clean, bounded representation of subprocess code execution.
    Preserves all errors and stack traces while bounding large stdout dumps.
    """
    sections: List[str] = []

    clean_stdout = stdout.strip() if stdout else ""
    clean_stderr = stderr.strip() if stderr else ""

    if clean_stdout:
        lines = clean_stdout.splitlines()
        if len(lines) > max_lines or len(clean_stdout) > max_chars:
            head_count = max_lines // 2
            tail_count = max_lines // 2
            head = "\n".join(lines[:head_count])
            tail = "\n".join(lines[-tail_count:])
            omitted = len(lines) - (head_count + tail_count)
            sections.append(
                f"STDOUT (Bounded Summary — {len(lines)} lines, {len(clean_stdout)} chars):\n"
                f"{head}\n"
                f"... [{omitted} lines omitted] ...\n"
                f"{tail}"
            )
        else:
            sections.append(f"STDOUT:\n{clean_stdout}")

    if clean_stderr:
        # Errors are NEVER truncated
        sections.append(f"STDERR / ERRORS:\n{clean_stderr}")

    if not sections:
        return "Execution completed successfully with no output."

    return "\n\n".join(sections)

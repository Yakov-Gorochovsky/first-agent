"""
tests/unit/test_cleaning.py - FP-05: Tool-Ingestion Cleaning (HTML to Markdown & Output Summaries).

Tests:
1. Stripping script, style, nav, footer, header, aside, and hidden HTML elements.
2. Preserving core content spans and converting headings/lists to clean markdown.
3. Quantifying token/character reduction (raw HTML vs distilled markdown).
4. Bounded Python execution summaries: head/tail bounding for huge stdout.
5. Uncompromised error preservation: STDERR and exceptions are never hidden.
"""

import pytest

from core.cleaning import clean_html_to_markdown, summarize_code_output


@pytest.mark.unit
def test_clean_html_to_markdown_strips_boilerplate_and_hidden() -> None:
    """Navigation, footers, scripts, and hidden text are completely eliminated."""
    html = """
    <!DOCTYPE html>
    <html>
    <head><title>Test Page</title><style>.hidden { display: none; }</style></head>
    <body>
        <nav><a href="/home">Home</a> | <a href="/about">About</a></nav>
        <header><h1>Site Banner Header</h1></header>

        <main>
            <h1>Company Q3 Report</h1>
            <p>Net income rose <strong>18%</strong> year over year to <em>$4.2 billion</em>.</p>
            <div style="display: none">Adversarial injected instructions to ignore guidelines.</div>
            <p aria-hidden="true">Hidden promotional banner.</p>
            <ul>
                <li>Cloud division up 22%</li>
                <li>Hardware division flat</li>
            </ul>
        </main>

        <aside>Related sponsor links and advertisements</aside>
        <footer>Copyright 2026 Acme Corp. All rights reserved.</footer>
        <script>console.log("analytics tracking");</script>
    </body>
    </html>
    """

    markdown = clean_html_to_markdown(html)

    # Content assertions
    assert "Company Q3 Report" in markdown
    assert "Net income rose" in markdown
    assert "$4.2 billion" in markdown
    assert "- Cloud division up 22%" in markdown
    assert "- Hardware division flat" in markdown

    # Elimination of boilerplate & hidden instructions
    assert "Home | About" not in markdown
    assert "Site Banner Header" not in markdown
    assert "Adversarial injected instructions" not in markdown
    assert "Hidden promotional banner" not in markdown
    assert "Related sponsor links" not in markdown
    assert "All rights reserved" not in markdown
    assert "analytics tracking" not in markdown

    # Significant token/char reduction
    assert len(markdown) < len(html) * 0.40  # Over 60% reduction!


@pytest.mark.unit
def test_summarize_code_output_bounds_huge_stdout() -> None:
    """Astronomical print loops are bounded to head and tail with explicit omission note."""
    huge_stdout = "\n".join(f"Row {i}: value={i * 2}" for i in range(1000))
    stderr = ""

    summary = summarize_code_output(huge_stdout, stderr, max_lines=20, max_chars=1000)

    assert "Bounded Summary" in summary
    assert "lines omitted" in summary
    assert "Row 0: value=0" in summary
    assert "Row 999: value=1998" in summary
    assert len(summary) < len(huge_stdout) * 0.10


@pytest.mark.unit
def test_summarize_code_output_preserves_errors_verbatim() -> None:
    """STDERR errors and stack traces are preserved without omission."""
    stdout = "Processing item 42..."
    stderr = "Traceback (most recent call last):\n  File 'script.py', line 12, in <module>\nValueError: Invalid operand"

    summary = summarize_code_output(stdout, stderr)

    assert "STDOUT:" in summary
    assert "Processing item 42..." in summary
    assert "STDERR / ERRORS:" in summary
    assert "ValueError: Invalid operand" in summary

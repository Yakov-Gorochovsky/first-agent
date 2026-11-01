"""swarm_tools/web.py - Time, weather, search, and SSRF-protected page fetching."""

import datetime
import re
from html.parser import HTMLParser
from typing import Dict, List
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

import requests

from core.config import CONFIG
from core.resilience import RetryConfig, retry_with_backoff
from swarm_tools.safety import _pinned_getaddrinfo, _resolve_and_validate_public_url

_DDG_LINK_RE = re.compile(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.DOTALL)
_DDG_SNIPPET_RE = re.compile(r'<a[^>]+class="result__snippet"[^>]*>(.*?)</a>', re.DOTALL)


class _TextExtractor(HTMLParser):
    """Strip HTML tags, scripts, and styles to extract readable text."""

    def __init__(self) -> None:
        super().__init__()
        self._skip_depth = 0
        self._chunks: List[str] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in {"script", "style", "noscript", "svg", "head"}:
            self._skip_depth += 1
        elif tag in {"p", "br", "div", "li", "tr", "h1", "h2", "h3", "h4"}:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript", "svg", "head"} and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            cleaned = data.strip()
            if cleaned:
                self._chunks.append(cleaned + " ")

    def get_text(self) -> str:
        raw = "".join(self._chunks)
        lines = [" ".join(line.split()) for line in raw.splitlines()]
        return "\n".join(line for line in lines if line)


def _html_to_text(html_content: str) -> str:
    parser = _TextExtractor()
    parser.feed(html_content)
    return parser.get_text()


def get_current_time() -> str:
    """Return the current local date, time, and day of the week."""
    now = datetime.datetime.now().astimezone()
    return now.strftime("%A, %Y-%m-%d %H:%M:%S (%Z)")


def get_weather(city: str) -> str:
    """Fetch current weather for a city using wttr.in (no API key needed)."""
    try:
        url = f"https://wttr.in/{requests.utils.quote(city)}?format=j1"
        resp = requests.get(
            url,
            timeout=CONFIG.weather_timeout_sec,
            headers={"User-Agent": "first-agent/1.0"},
        )
        resp.raise_for_status()
        data = resp.json()
        current = data["current_condition"][0]
        desc = current["weatherDesc"][0]["value"]
        temp_c = current["temp_C"]
        feels_c = current["FeelsLikeC"]
        humidity = current["humidity"]
        return (
            f"Weather in {city}: {desc}, {temp_c}°C "
            f"(feels like {feels_c}°C), Humidity: {humidity}%"
        )
    except Exception as exc:
        return f"Could not fetch live weather for '{city}': {exc}"


def web_search(query: str) -> str:
    """Search the web using DuckDuckGo HTML and return top results."""
    try:
        url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            )
        }

        def _do_search() -> requests.Response:
            r = requests.get(url, headers=headers, timeout=CONFIG.search_timeout_sec)
            r.raise_for_status()
            return r

        resp = retry_with_backoff(
            _do_search,
            retry_config=RetryConfig(max_retries=3, initial_delay=1.0, max_delay=15.0),
            retryable_exceptions=(requests.RequestException,),
        )
        html = resp.text

        links = _DDG_LINK_RE.findall(html)
        snippets = _DDG_SNIPPET_RE.findall(html)

        results = []
        for idx, (raw_href, raw_title) in enumerate(links[: CONFIG.search_max_results], start=1):
            title = _html_to_text(raw_title).strip()
            snippet = _html_to_text(snippets[idx - 1]).strip() if idx - 1 < len(snippets) else ""
            parsed_href = urlparse(raw_href)
            qs = parse_qs(parsed_href.query)
            clean_url = unquote(qs["uddg"][0]) if "uddg" in qs else raw_href
            results.append(f"{idx}. {title}\n   URL: {clean_url}\n   Snippet: {snippet}")

        if not results:
            return f"No web results found for '{query}'."
        return "\n\n".join(results)
    except Exception as exc:
        return f"Error searching the web for '{query}': {exc}"


class _DOMStructureInspector(HTMLParser):
    """Extract structural DOM/HTML metadata (html attrs, meta, forms, inputs, scripts, links, iframes, aria/alt stats)."""

    def __init__(self) -> None:
        super().__init__()
        self.html_attrs: Dict[str, str] = {}
        self.title: str = ""
        self._in_title = False
        self.metas: List[str] = []
        self.forms: List[str] = []
        self.inputs: List[str] = []
        self.buttons: List[str] = []
        self.scripts: List[str] = []
        self.stylesheets: List[str] = []
        self.iframes: List[str] = []
        self.img_total = 0
        self.img_missing_alt = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        ad = {k.lower(): (v or "") for k, v in attrs if k}
        t = tag.lower()
        if t == "html":
            self.html_attrs = {k: v for k, v in ad.items() if k in ("lang", "dir", "class")}
        elif t == "title":
            self._in_title = True
        elif t == "meta" and len(self.metas) < 12:
            name = ad.get("name") or ad.get("property") or ad.get("http-equiv") or "charset"
            content = ad.get("content") or ad.get("charset") or ""
            self.metas.append(f"<meta {name}={content!r}>")
        elif t == "form" and len(self.forms) < 8:
            self.forms.append(
                f"<form action={ad.get('action', '')!r} method={ad.get('method', 'get')!r} id={ad.get('id', '')!r}>"
            )
        elif t in ("input", "select", "textarea") and len(self.inputs) < 15:
            self.inputs.append(
                f"<{t} type={ad.get('type', 'text')!r} name={ad.get('name', '')!r} id={ad.get('id', '')!r} "
                f"autocomplete={ad.get('autocomplete', '')!r} aria-label={ad.get('aria-label', '')!r} required={'required' in ad}>"
            )
        elif t == "button" and len(self.buttons) < 10:
            self.buttons.append(
                f"<button type={ad.get('type', '')!r} id={ad.get('id', '')!r} aria-label={ad.get('aria-label', '')!r}>"
            )
        elif t == "script" and ad.get("src") and len(self.scripts) < 12:
            self.scripts.append(ad["src"])
        elif t == "link" and ad.get("href") and len(self.stylesheets) < 10:
            self.stylesheets.append(f"{ad.get('rel', 'link')}: {ad['href']}")
        elif t == "iframe" and len(self.iframes) < 6:
            self.iframes.append(ad.get("src", "<inline-iframe>"))
        elif t == "img":
            self.img_total += 1
            if "alt" not in ad:
                self.img_missing_alt += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title and not self.title:
            self.title = data.strip()[:120]

    def format_summary(self) -> str:
        lines: List[str] = []
        if self.html_attrs:
            lines.append(f"- <html> attrs: {self.html_attrs}")
        if self.title:
            lines.append(f"- <title>: {self.title}")
        if self.metas:
            lines.append(f"- <meta> tags ({len(self.metas)}): " + "; ".join(self.metas))
        if self.forms:
            lines.append(f"- <form> tags ({len(self.forms)}): " + "; ".join(self.forms))
        if self.inputs:
            lines.append(f"- Form controls ({len(self.inputs)}): " + "; ".join(self.inputs))
        if self.buttons:
            lines.append(f"- <button> tags ({len(self.buttons)}): " + "; ".join(self.buttons))
        if self.scripts:
            lines.append(f"- External <script src> ({len(self.scripts)}): " + ", ".join(self.scripts))
        if self.stylesheets:
            lines.append(f"- <link> assets ({len(self.stylesheets)}): " + ", ".join(self.stylesheets))
        if self.iframes:
            lines.append(f"- <iframe> sources ({len(self.iframes)}): " + ", ".join(self.iframes))
        if self.img_total > 0:
            lines.append(f"- <img> count: {self.img_total} (missing alt: {self.img_missing_alt})")
        return "\n".join(lines)


def _extract_dom_metadata(html_content: str) -> str:
    if "<" not in html_content:
        return ""
    inspector = _DOMStructureInspector()
    try:
        inspector.feed(html_content[:100_000])
        return inspector.format_summary()
    except Exception:
        return ""


def fetch_url(url: str, include_raw: bool = False) -> str:
    """Fetch a public webpage URL (SSRF & DNS-rebinding protected) and return headers, structural DOM metadata, and readable or raw content."""
    try:
        safe_url, hostname, pinned_ip, port = _resolve_and_validate_public_url(url)
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            )
        }
        with _pinned_getaddrinfo(hostname, pinned_ip):
            def _do_fetch() -> requests.Response:
                r = requests.get(
                    safe_url,
                    headers=headers,
                    timeout=CONFIG.fetch_timeout_sec,
                    allow_redirects=False,
                )
                r.raise_for_status()
                return r

            resp = retry_with_backoff(
                _do_fetch,
                retry_config=RetryConfig(max_retries=3, initial_delay=1.0, max_delay=15.0),
                retryable_exceptions=(requests.RequestException,),
            )
        raw_body = getattr(resp, "text", "") or ""
        from core.cleaning import clean_html_to_markdown
        text = clean_html_to_markdown(raw_body, max_chars=CONFIG.fetch_max_chars)
        dom_meta = _extract_dom_metadata(raw_body) if isinstance(raw_body, str) else ""
        status_line = f"HTTP {getattr(resp, 'status_code', 200)} {getattr(resp, 'reason', '')}".strip()
        headers_lines = [f"{k}: {v}" for k, v in getattr(resp, "headers", {}).items()]
        headers_block = "\n".join(headers_lines[:30])
        content_preview = text if text else "No readable text content found."
        sections = [
            f"[HTTP RESPONSE: {status_line}]",
            f"[RESPONSE HEADERS]\n{headers_block}",
        ]
        if dom_meta:
            sections.append(f"[STRUCTURAL DOM / HTML METADATA]\n{dom_meta}")
        sections.append(f"[PAGE CONTENT]\n{content_preview}")
        if include_raw and isinstance(raw_body, str) and raw_body.strip():
            sections.append(f"[RAW RESPONSE BODY SNIPPET]\n{raw_body[:CONFIG.fetch_max_chars]}")
        return "\n\n".join(sections)
    except Exception as exc:
        return f"Error fetching URL '{url}': {exc}"

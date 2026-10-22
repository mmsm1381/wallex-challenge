"""Downloads the Wallex API documentation as plain text (task 2.1).

developers.wallex.ir is a Docusaurus site. Each page has a pre-rendered
HTML file, and `sitemap.xml` lists all pages. The script reads the sitemap,
downloads each page, removes the HTML tags and writes one Markdown file
with all pages. It needs no API key.

Usage (standard library only, run where developers.wallex.ir answers):
    python3 -I research/scripts/wallex_docs_fetch.py wallex_docs.md
"""

import html
import html.parser
import re
import sys
import time
import urllib.error
import urllib.request

SITE = "https://developers.wallex.ir"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
# Bypass the dead ALL_PROXY of the developer machine (AGENTS.md section 5).
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def fetch(url: str) -> str:
    """Returns the body of `url` as text."""
    request = urllib.request.Request(url, headers={"User-Agent": UA})
    with OPENER.open(request, timeout=30) as response:
        return response.read().decode("utf-8", "replace")


class _Text(html.parser.HTMLParser):
    """Collects the text of the main article. Keeps code blocks and tables."""

    _BLOCKS = {"p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "pre", "br",
               "table", "section"}

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0
        self._article = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "nav", "footer", "svg"):
            self._skip += 1
        if tag in ("article", "main"):
            self._article += 1
        if tag in ("h1", "h2", "h3", "h4"):
            self.parts.append("\n\n" + "#" * int(tag[1]) + " ")
        elif tag in ("td", "th"):
            self.parts.append(" | ")
        elif tag == "pre":
            self.parts.append("\n```\n")
        elif tag in self._BLOCKS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "nav", "footer", "svg"):
            self._skip = max(0, self._skip - 1)
        if tag in ("article", "main"):
            self._article = max(0, self._article - 1)
        if tag == "pre":
            self.parts.append("\n```\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def page_text(page_html: str) -> str:
    """Returns the readable text of one docs page."""
    parser = _Text()
    parser.feed(page_html)
    text = html.unescape("".join(parser.parts))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def main(out_path: str) -> None:
    """Downloads the sitemap and all pages, and writes one Markdown file."""
    sitemap = fetch(f"{SITE}/sitemap.xml")
    urls = re.findall(r"<loc>([^<]+)</loc>", sitemap)
    print(f"{len(urls)} pages in the sitemap", file=sys.stderr)
    sections = [f"# Wallex API documentation\n\nSource: {SITE}, "
                f"downloaded {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}\n"]
    for url in urls:
        print(url, file=sys.stderr)
        try:
            sections.append(f"\n\n---\n\nURL: {url}\n\n{page_text(fetch(url))}")
        except (OSError, urllib.error.URLError) as error:
            sections.append(f"\n\n---\n\nURL: {url}\n\nERROR: {error}")
        time.sleep(0.3)
    with open(out_path, "w", encoding="utf-8") as out_file:
        out_file.write("".join(sections))
    print(f"wrote {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "wallex_docs.md")

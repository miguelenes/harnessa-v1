from __future__ import annotations

import sys
from html.parser import HTMLParser
from pathlib import Path


class MarketingPage(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[str] = []
        self.attributes: list[dict[str, str | None]] = []
        self.text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append(tag)
        self.attributes.append(dict(attrs))

    def handle_data(self, data: str) -> None:
        self.text.append(data)


def main() -> int:
    page_path = Path("apps/marketing/dist/index.html")
    if not page_path.is_file():
        print(f"Marketing build output is missing: {page_path}", file=sys.stderr)
        return 1

    html = page_path.read_text(encoding="utf-8")
    page = MarketingPage()
    page.feed(html)
    text = " ".join(page.text)
    anchors = [attrs for tag, attrs in zip(page.tags, page.attributes) if tag == "a"]
    headings = [attrs for tag, attrs in zip(page.tags, page.attributes) if tag == "h1"]
    navigation = [attrs for tag, attrs in zip(page.tags, page.attributes) if tag == "nav"]
    failures = []
    if not any(tag == "main" for tag in page.tags):
        failures.append("main landmark is missing")
    if len(headings) != 1:
        failures.append(f"expected one page heading, found {len(headings)}")
    if not any(attrs.get("aria-label") == "Main navigation" for attrs in navigation):
        failures.append("labeled main navigation is missing")
    if not any(attrs.get("href") == "#main" for attrs in anchors):
        failures.append("skip link to main content is missing")
    if "Agentic engineering" not in text or "kept in the loop" not in text:
        failures.append("product headline is missing")
    if "https://app.harnessa.cloud" not in html:
        failures.append("cloud preview destination is missing")
    if "href={" in html or "<astro" in html:
        failures.append("unresolved template markup remains in the generated page")
    if failures:
        for failure in failures:
            print(f"Marketing artifact check failed: {failure}", file=sys.stderr)
        return 1
    print("Marketing static artifact checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Makes the PDF of every installer's guide in
src/localhome/web/static/guide/ - the HTML is the source, the PDF next to
it is what the admin panel offers for download. Run it after changing a
guide, and commit both:

    python tools/build_guides.py

Needs Playwright's Chromium (pip install -e ".[ui]" and
python -m playwright install chromium) - on a PC, not on the board.
"""
from __future__ import annotations

from pathlib import Path

from playwright.sync_api import sync_playwright

GUIDES = Path(__file__).resolve().parents[1] / "src" / "localhome" / "web" / "static" / "guide"


def main() -> None:
    sources = sorted(GUIDES.glob("*.html"))
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        for source in sources:
            page.goto(source.as_uri())
            target = source.with_suffix(".pdf")
            page.pdf(path=str(target), format="A4", print_background=True, prefer_css_page_size=True)
            print(f"{target.relative_to(GUIDES.parents[3])}: {target.stat().st_size // 1024} KB")
        browser.close()


if __name__ == "__main__":
    main()

"""Render docs/images/social-preview.png (1280x640) from social_preview.html.

GitHub shows this image when the repo link is shared (Settings > General > Social
preview). The HTML template is the source of truth; the PNG is a build output.

    uv run --with playwright python scripts/readme_media/render_social.py
"""

from __future__ import annotations

from _common import HERE, IMAGES, launch, route_web_fonts
from playwright.sync_api import sync_playwright

TEMPLATE = HERE / "social_preview.html"
OUT = IMAGES / "social-preview.png"


def main() -> None:
    with sync_playwright() as pw:
        browser = launch(pw)
        page = browser.new_page(viewport={"width": 1280, "height": 640}, device_scale_factor=1)
        route_web_fonts(page)
        page.goto(TEMPLATE.as_uri(), wait_until="networkidle")
        page.evaluate("document.fonts.ready")
        page.screenshot(path=str(OUT), clip={"x": 0, "y": 0, "width": 1280, "height": 640})
        browser.close()
    print(f"wrote {OUT.relative_to(IMAGES.parent.parent)}")


if __name__ == "__main__":
    main()

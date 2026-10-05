#!/usr/bin/env python3
"""Re-render the README's Excalidraw diagrams to PNG, reproducibly.

Why this exists. The diagrams in ``docs/images/*.excalidraw`` are the source of
truth and the PNGs beside them are what GitHub shows. Exporting by hand from
excalidraw.com drifts: a different zoom, padding or font each time, and a label
fixed in the JSON but not in the PNG (or the reverse) misleads every reader of
the README. This script renders every diagram the same way, so after editing the
JSON one command brings the PNG back in line.

How it renders. Excalidraw's own ``exportToSvg`` (from ``@excalidraw/utils``)
draws the scene in headless Chromium driven by Playwright, so shapes, arrows and
bindings look exactly as they do in the editor. Two choices keep the look of the
existing diagrams:

- The canvas colour is the file's ``appState.viewBackgroundColor`` (the dark
  ``#0a0a0a`` the README diagrams use), exported with the background on.
- Text is set in Space Grotesk (``--font``) instead of Excalidraw's built-in
  font for the elements' ``fontFamily``, because that is the face the README
  diagrams were drawn in. Positions and alignment still come from the JSON.

All network access happens in Python, never in the browser: the library comes
from jsDelivr (or ``--lib-dir``, a local ``npm install @excalidraw/utils@0.1.5``
for machines that cannot reach the CDN) and the font from Google Fonts, then
both are served to the page through Playwright request routing. That keeps the
browser offline, so a proxy or certificate store the browser does not share
with Python cannot break the export.

Usage (dev tool, not part of the package)::

    uv run --with playwright python scripts/readme_media/export_excalidraw.py \\
        docs/images/skills-flow.excalidraw docs/images/repos-flow.excalidraw

    # CDN blocked? Install the library locally once and point at it:
    npm install --prefix /tmp/exlib @excalidraw/utils@0.1.5
    uv run --with playwright python scripts/readme_media/export_excalidraw.py \\
        --lib-dir /tmp/exlib/node_modules/@excalidraw/utils docs/images/*.excalidraw

Each ``<name>.excalidraw`` is written to ``<name>.png`` beside it (or into
``--out-dir``). Chromium: ``$STREAMSNOW_CHROMIUM``, a preinstalled Playwright
browser, or the one from ``playwright install chromium``. With Pillow available
(``--with pillow``), a PNG over ``--max-kb`` is re-saved as a 256-colour palette
image, which these flat-colour diagrams survive without visible change.
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import re
import sys
import tempfile
import urllib.request
from pathlib import Path

from _common import find_chromium

UTILS_VERSION = "0.1.5"
UTILS_CDN = f"https://cdn.jsdelivr.net/npm/@excalidraw/utils@{UTILS_VERSION}/dist/prod/index.js"
FONTS_CSS = "https://fonts.googleapis.com/css2?family={family}:wght@400;500;600&display=block"
# A made-up origin the page and library are served from via request routing.
ORIGIN = "https://excalidraw-export.invalid"

PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><style>
{font_css}
html, body {{ margin: 0; padding: 0; background: {background}; }}
#out svg {{ display: block; }}
</style></head>
<body><div id="out"></div></body></html>
"""

RENDER_JS = """
async ({scene, padding, font}) => {
  const lib = await import("__ORIGIN__/lib/index.js");
  const svg = await lib.exportToSvg({
    data: {
      elements: scene.elements,
      appState: {
        ...scene.appState,
        exportBackground: true,
        exportWithDarkMode: false,
        exportScale: 1,
        viewBackgroundColor: scene.appState.viewBackgroundColor || "#ffffff",
      },
      files: scene.files || {},
    },
    config: { padding, skipInliningFonts: true },
  });
  // Excalidraw's per-family font names -> the diagram face. Layout stays as exported.
  svg.querySelectorAll("text").forEach((t) => {
    t.setAttribute("font-family", `'${font}', sans-serif`);
  });
  svg.querySelectorAll("style.style-fonts").forEach((s) => s.remove());
  const out = document.getElementById("out");
  out.innerHTML = "";
  out.appendChild(svg);
  await document.fonts.load(`16px '${font}'`);
  await document.fonts.ready;
  return [svg.getAttribute("width"), svg.getAttribute("height")];
}
""".replace("__ORIGIN__", ORIGIN)


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "streamsnow-readme-media"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.read()


def library_source(lib_dir: Path | None) -> bytes:
    """The @excalidraw/utils ESM bundle: from --lib-dir, a temp-dir cache, or the CDN."""
    if lib_dir is not None:
        candidates = [lib_dir / "dist" / "prod" / "index.js", lib_dir / "index.js"]
        for path in candidates:
            if path.is_file():
                return path.read_bytes()
        raise SystemExit(f"no dist/prod/index.js under {lib_dir}")
    cache = Path(tempfile.gettempdir()) / f"excalidraw-utils-{UTILS_VERSION}.js"
    if cache.is_file():
        return cache.read_bytes()
    data = fetch(UTILS_CDN)
    cache.write_bytes(data)
    return data


def font_css(family: str) -> str:
    """Google Fonts CSS for ``family`` with each font file inlined as a data URI."""
    css = fetch(FONTS_CSS.format(family=family.replace(" ", "+"))).decode("utf-8")

    def inline(match: re.Match[str]) -> str:
        url = match.group(1)
        kind = "font/woff2" if url.endswith(".woff2") else "font/ttf"
        return f"url(data:{kind};base64,{base64.b64encode(fetch(url)).decode('ascii')})"

    return re.sub(r"url\((https://[^)]+)\)", inline, css)


def shrink(png: bytes, max_kb: int) -> bytes:
    """Re-save as a 256-colour palette PNG when over budget (needs Pillow)."""
    if len(png) <= max_kb * 1024:
        return png
    try:
        from PIL import Image, ImageFile
    except ImportError:
        print(f"  note: {len(png) // 1024} KB > {max_kb} KB; add --with pillow to shrink")
        return png
    parser = ImageFile.Parser()
    parser.feed(png)
    img = parser.close().convert("RGB")
    buf = io.BytesIO()
    img.quantize(colors=256, method=Image.Quantize.MEDIANCUT).save(buf, "PNG", optimize=True)
    smaller = buf.getvalue()
    return smaller if len(smaller) < len(png) else png


def export(paths: list[Path], args: argparse.Namespace) -> None:
    from playwright.sync_api import sync_playwright

    lib = library_source(args.lib_dir)
    css = font_css(args.font)
    lib_dir = args.lib_dir

    def serve(route) -> None:
        url = route.request.url
        if url.startswith(f"{ORIGIN}/lib/index.js"):
            route.fulfill(status=200, body=lib, content_type="text/javascript")
        elif url.startswith(f"{ORIGIN}/lib/") and lib_dir is not None:
            asset = lib_dir / "dist" / "prod" / url[len(f"{ORIGIN}/lib/") :].split("?")[0]
            if asset.is_file():
                route.fulfill(status=200, body=asset.read_bytes())
            else:
                route.fulfill(status=404, body=b"")
        elif url.startswith(f"{ORIGIN}/page/"):
            name = url.rsplit("/", 1)[-1]
            route.fulfill(status=200, body=pages[name], content_type="text/html")
        else:
            # Keep the browser offline: everything it needs is served above.
            route.abort()

    pages: dict[str, str] = {}
    exe = find_chromium()
    with sync_playwright() as pw:
        browser = pw.chromium.launch(**({"executable_path": exe} if exe else {}))
        try:
            for path in paths:
                scene = json.loads(path.read_text(encoding="utf-8"))
                if scene.get("type") != "excalidraw":
                    raise SystemExit(f"{path}: not an Excalidraw scene")
                bg = scene.get("appState", {}).get("viewBackgroundColor") or "#ffffff"
                pages[path.stem] = PAGE.format(font_css=css, background=bg)
                ctx = browser.new_context(device_scale_factor=args.scale)
                page = ctx.new_page()
                page.route("**/*", serve)
                page.goto(f"{ORIGIN}/page/{path.stem}")
                size = page.evaluate(
                    RENDER_JS, {"scene": scene, "padding": args.padding, "font": args.font}
                )
                png = page.locator("#out svg").screenshot(type="png", omit_background=False)
                ctx.close()
                png = shrink(png, args.max_kb)
                out_dir = args.out_dir or path.parent
                out = out_dir / f"{path.stem}.png"
                out.write_bytes(png)
                w, h = (round(float(v) * args.scale) for v in size)
                print(f"{out}  {w}x{h}  {len(png) // 1024} KB")
        finally:
            browser.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("files", nargs="+", type=Path, help=".excalidraw files to render")
    parser.add_argument("--scale", type=float, default=2.0, help="pixel ratio (default 2)")
    parser.add_argument("--padding", type=int, default=24, help="export padding in scene units")
    parser.add_argument("--font", default="Space Grotesk", help="Google Fonts family for text")
    parser.add_argument("--lib-dir", type=Path, help="local @excalidraw/utils package dir")
    parser.add_argument("--out-dir", type=Path, help="write PNGs here instead of beside the JSON")
    parser.add_argument("--max-kb", type=int, default=500, help="palette-shrink PNGs above this")
    args = parser.parse_args(argv)
    missing = [p for p in args.files if not p.is_file()]
    if missing:
        parser.error(f"not found: {', '.join(map(str, missing))}")
    if args.out_dir:
        args.out_dir.mkdir(parents=True, exist_ok=True)
    export(args.files, args)
    return 0


if __name__ == "__main__":
    sys.exit(main())

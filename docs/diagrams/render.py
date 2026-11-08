#!/usr/bin/env python3
"""Renders docs/diagrams/*.mmd to docs/images/*.svg with fixed colours that read on light and dark pages.

  python3 docs/diagrams/render.py      (needs Playwright with a Chromium; loads Mermaid from jsDelivr)
"""
import pathlib
import sys

from playwright.sync_api import sync_playwright

HERE = pathlib.Path(__file__).resolve().parent
OUT = HERE.parent / "images"
CONFIG = {
    "startOnLoad": False,
    "theme": "base",
    "htmlLabels": False,
    "flowchart": {"htmlLabels": False, "curve": "basis", "padding": 14},
    "themeVariables": {
        "fontFamily": "Inter, Segoe UI, Helvetica, Arial, sans-serif",
        "fontSize": "16px",
        "primaryColor": "#3a5bd9",
        "primaryTextColor": "#5b6b94",
        "primaryBorderColor": "#3a5bd9",
        "lineColor": "#8796bd",
        "textColor": "#5b6b94",
        "edgeLabelBackground": "transparent",
        "background": "transparent",
    },
}

with sync_playwright() as p:
    browser = p.chromium.launch(executable_path=sys.argv[1] if len(sys.argv) > 1 else None, headless=True)
    page = browser.new_page()
    page.set_content("<!doctype html><body></body>")
    page.add_script_tag(url="https://cdn.jsdelivr.net/npm/mermaid@11.4.1/dist/mermaid.min.js")
    for src in sorted(HERE.glob("*.mmd")):
        svg = page.evaluate(
            """async ([cfg, code]) => {
                mermaid.initialize(cfg);
                const { svg } = await mermaid.render('d', code);
                document.body.innerHTML = svg;   // serialised again below as well-formed XML for an <img>
                return new XMLSerializer().serializeToString(document.body.querySelector('svg'));
            }""",
            [CONFIG, src.read_text()],
        )
        (OUT / f"{src.stem}.svg").write_text(svg)
        print("rendered", src.stem)
    browser.close()

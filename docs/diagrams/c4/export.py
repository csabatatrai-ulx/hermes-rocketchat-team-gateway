#!/usr/bin/env python3
"""Exports the C4 views of workspace.dsl to docs/images/c4-*.svg.

Start Structurizr on this directory first:
  docker run -d --name c4-local -u "$(id -u):$(id -g)" -p 127.0.0.1:8090:8080 \
    -v "$PWD/docs/diagrams/c4:/usr/local/structurizr:Z" structurizr/structurizr local
then: python3 docs/diagrams/c4/export.py [path to Chrome]
"""
import pathlib
import sys

from playwright.sync_api import sync_playwright

OUT = pathlib.Path(__file__).resolve().parents[2] / "images"
VIEWS = (("SystemContext", "c4-context"), ("Containers", "c4-containers"), ("Components", "c4-components"))

with sync_playwright() as p:
    browser = p.chromium.launch(executable_path=sys.argv[1] if len(sys.argv) > 1 else None, headless=True)
    page = browser.new_context(viewport={"width": 1600, "height": 1000}, bypass_csp=True).new_page()
    for key, name in VIEWS:
        page.goto(f"http://127.0.0.1:8090/workspace/1/diagrams#{key}", wait_until="load")
        page.wait_for_function(
            f"window.structurizr && structurizr.scripting && structurizr.scripting.isDiagramRendered()"
            f" && structurizr.scripting.getViewKey() == '{key}'", timeout=60000)
        page.wait_for_timeout(1200)
        (OUT / f"{name}.svg").write_text(page.evaluate("structurizr.scripting.exportCurrentDiagramToSVG({includeMetadata: true})"))
        print("exported", name)
    browser.close()

"""Fail the build if the built site links to a page it did not produce.

    Python scripts/build_site.py && python scripts/check_site_links.py

Every internal `href` is resolved against `dist/site/` on disk. A renamed
reference document, a deleted example or a nav entry pointing at a slug that
drifted from `site_render.PAGE_FOR` becomes a 404 that nobody notices until
a reader hits it: cheap to check, so it is checked on every deploy.

External links are counted and their hosts printed, not fetched: the site
must stay buildable offline, and a network check would make this gate flaky
for no gain.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import unquote

SITE = Path(__file__).resolve().parents[1] / "dist" / "site"
_HREF = re.compile(r'(?:href|src)="([^"]+)"')


def main() -> int:
    if not SITE.is_dir():
        print(f"no built site at {SITE}: run scripts/build_site.py first")
        return 2

    pages = sorted(SITE.rglob("*.html"))
    broken: list[str] = []
    hosts: set[str] = set()

    for page in pages:
        for href in _HREF.findall(page.read_text(encoding="utf-8")):
            if "://" in href:
                hosts.add(href.split("/")[2])
                continue
            if href.startswith(("#", "mailto:", "data:")):
                continue
            target = unquote(href.split("#")[0])
            if not target:
                continue
            if not (page.parent / target).resolve().exists():
                broken.append(f"{page.relative_to(SITE)} -> {href}")

    print(f"{len(pages)} pages, {len(hosts)} external host(s): "
          f"{', '.join(sorted(hosts))}")
    if broken:
        print(f"{len(broken)} broken internal link(s):")
        for entry in broken:
            print(f"  {entry}")
        return 1
    print("no broken internal links")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""
Compile the two pages' JavaScript and write static/dist/ (see assets.py). Run it after changing a page:

    pip install mini-racer
    python scripts/build_assets.py

The Docker build runs it in its first stage. A server also rebuilds a stale page by itself at start-up
when mini-racer is installed; without it, a stale page stops the server with this instruction.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import hr_info  # noqa: E402  (importing a portal builds its pages)
import react_onboard  # noqa: E402

for name, built in (("onboard", react_onboard.ASSETS), ("hr", hr_info.ASSETS)):
    print(name, ", ".join(f"{file} ({len(text) // 1024} KB)" for file, text in sorted(built.files.items())))

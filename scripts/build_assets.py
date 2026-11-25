"""
Compile the two pages' JavaScript and write src/hr_onboarding/static/dist/ (see assets.py). Run it after changing a page:

    pip install mini-racer
    python scripts/build_assets.py

The Docker build runs it in its first stage. A server also rebuilds a stale page by itself at start-up
when mini-racer is installed; without it, a stale page stops the server with this instruction.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from hr_onboarding import hr_portal  # noqa: E402  (importing a portal builds its pages)
from hr_onboarding import employee_portal  # noqa: E402

for name, built in (("onboard", employee_portal.ASSETS), ("hr", hr_portal.ASSETS)):
    print(name, ", ".join(f"{file} ({len(text) // 1024} KB)" for file, text in sorted(built.files.items())))

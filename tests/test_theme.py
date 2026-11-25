import asyncio
import importlib
import os
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

from hr_onboarding import theme


def served(module_name):
    """The assembled page before its CSS and script become files (assets.py), or skip if dependencies are missing."""

    with mock.patch.dict(os.environ, {"STORAGE_BACKEND": "local"}):
        try:
            module = importlib.import_module(module_name)
        except ImportError as error:
            raise unittest.SkipTest(f"{module_name} needs {error.name}")

    return module.render_page() if module_name == "hr_onboarding.employee_portal" else module.theme.inject(module.PAGE)


class ThemeInjectTests(unittest.TestCase):

    def test_inject_replaces_every_marker(self):
        page = theme.inject("<!-- __THEME_HEAD__ -->/* __THEME_CSS__ */// __THEME_SCRIPT__")
        self.assertNotIn("__THEME_", page)
        self.assertIn("getThemeMode", page)
        self.assertIn(".theme-toggle", page)
        self.assertIn("function ThemeToggle", page)

    def test_three_modes(self):
        labels = re.findall(r'\["(system|light|dark)", "(Default|Light|Dark)"', theme.SCRIPT)
        self.assertEqual(labels, [("system", "Default"), ("light", "Light"), ("dark", "Dark")])


class FrontendThemeTests(unittest.TestCase):

    def check(self, module_name, light_var, dark_var):
        page = served(module_name)

        self.assertNotIn("__THEME_", page)
        # Applied before the stylesheet and the app, so there is no flash of the wrong theme
        self.assertLess(page.index("getThemeMode"), page.index("<style>"))
        self.assertLess(page.index("getThemeMode"), page.index("react.production"))
        # Dark colours exist, and the toggle is rendered
        dark = page[page.index(':root[data-theme="dark"] {'):]
        dark = dark[:dark.index("}")]
        self.assertIn(light_var, dark)
        self.assertIn(dark_var, dark)
        self.assertIn("<ThemeToggle", page)

    def test_onboarding_portal(self):
        self.check("hr_onboarding.employee_portal", "--surface:", "--bg:")

    def test_hr_portal(self):
        self.check("hr_onboarding.hr_portal", "--card:", "--bg:")

    def test_no_light_only_white_backgrounds_left(self):
        # Surfaces must use a variable so they flip in dark mode
        for module_name in ("hr_onboarding.employee_portal", "hr_onboarding.hr_portal"):
            css = served(module_name).split("<style>")[1].split("</style>")[0]
            # the company logo is an image on a white background: it keeps a white chip in dark mode too
            allowed = ("lightbox img", "viewer-frame", "toggle", "-logo", ".logo")
            for line in css.splitlines():
                if re.search(r"background:\s*#fff\b", line) and not any(name in line for name in allowed):
                    self.fail(f"{module_name}: hard-coded white background: {line.strip()[:100]}")


if __name__ == "__main__":
    unittest.main()

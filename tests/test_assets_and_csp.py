"""The pages' own files (assets.py) and the security headers (security_headers.py).

Under script-src 'self' nothing may be inline and nothing may come from another site. These tests build
both portals' pages and check that, run the compiled JavaScript through V8 to prove it is plain
JavaScript (not JSX), and check the headers the servers send.
"""

import importlib
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from py_mini_racer import MiniRacer

from hr_onboarding import assets
from hr_onboarding import security_headers
from test_dynamo_store import DOCS, ENV, REGION, AwsTestCase, HR_USERS, HR_SECRET

BUCKET_ORIGIN = f"https://{DOCS}.s3.{REGION}.amazonaws.com"
HEADERS = ("content-security-policy", "x-content-type-options", "referrer-policy", "strict-transport-security")


def fresh(module_name, env):
    sys.modules.pop(module_name, None)
    return importlib.import_module(module_name)


class BuiltPagesTests(unittest.TestCase):
    """Build every page into a temporary folder and inspect the result."""

    @classmethod
    def setUpClass(cls):
        local = {key: value for key, value in ENV.items() if key in ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP")}
        cls.patch = mock.patch.dict(os.environ, {**local, "HR_USERS": json.dumps(HR_USERS), "SESSION_SECRET": HR_SECRET}, clear=True)
        cls.patch.start()
        cls.onboard, cls.hr = fresh("hr_onboarding.employee_portal", {}), fresh("hr_onboarding.hr_portal", {})
        cls.folder = tempfile.TemporaryDirectory()
        cls.sets = {
            "onboard": assets.AssetSet("onboard", {"index": cls.onboard.render_page}, dist=cls.folder.name),
            "hr": assets.AssetSet("hr", {
                "hr": lambda: cls.hr.theme.inject(cls.hr.PAGE), "login": lambda: cls.hr.theme.inject(cls.hr.LOGIN_PAGE)},
                dist=cls.folder.name),
        }

    @classmethod
    def tearDownClass(cls):
        cls.patch.stop()
        cls.folder.cleanup()
        sys.modules.pop("hr_onboarding.employee_portal", None)
        sys.modules.pop("hr_onboarding.hr_portal", None)

    def pages(self):
        return [(f"{name}/{page}", html) for name, built in self.sets.items() for page, html in built.html.items()]

    def test_every_page_was_built(self):
        self.assertEqual({label for label, _ in self.pages()}, {"onboard/index", "hr/hr", "hr/login"})

    def test_no_inline_script_style_or_handler_is_left(self):
        for label, html in self.pages():
            with self.subTest(label):
                self.assertEqual(re.findall(r"<script(?![^>]*\bsrc=)[^>]*>", html), [], "inline <script>")
                self.assertNotIn("<style", html)
                self.assertEqual(re.findall(r"\sstyle=", html), [], "style attribute")
                self.assertEqual(re.findall(r"\son[a-z]+=", html), [], "inline event handler")
                self.assertNotIn("text/babel", html)

    def test_nothing_is_loaded_from_another_site(self):
        for label, html in self.pages():
            with self.subTest(label):
                self.assertEqual(re.findall(r'(?:src|href)="https?://[^"]*"', html), [])
                for host in ("unpkg.com", "cdnjs", "googleapis", "gstatic", "jsdelivr"):
                    self.assertNotIn(host, html)

    def test_every_script_and_style_link_points_at_our_own_files(self):
        for label, html in self.pages():
            for url in re.findall(r'<script[^>]*src="([^"]+)"', html) + re.findall(r'<link[^>]*rel="stylesheet"[^>]*href="([^"]+)"', html):
                with self.subTest(label, url=url):
                    self.assertTrue(url.startswith("/assets/"), url)
                    built = self.sets["hr" if label.startswith("hr/") else "onboard"]
                    path = url.split("?")[0][len("/assets/"):]
                    self.assertTrue(path in built.files or path.removeprefix("vendor/") in built.vendor, path)

    def test_the_compiled_javascript_is_plain_javascript(self):
        for set_name, built in self.sets.items():
            for file, code in built.files.items():
                if not file.endswith(".js"):
                    continue
                with self.subTest(file):
                    self.assertNotRegex(code, r"<[A-Z][A-Za-z]*\s[^>]*/>", "JSX left in the file")
                    MiniRacer().eval("(function () {" + code + "\n})")  # parses; a JSX tag would be a syntax error

    def test_the_theme_script_still_runs_first_so_there_is_no_flash(self):
        for built, page in ((self.sets["onboard"], "index"), (self.sets["hr"], "hr")):
            html = built.html[page]
            self.assertIn("getThemeMode", built.files[f"{page}.script0.js"])
            self.assertLess(html.index(f"{page}.script0.js"), html.index("react-18.3.1"))
            self.assertLess(html.index(f"{page}.script0.js"), html.index(f"{page}.css"))

    def test_the_browsers_do_not_need_babel_or_eval(self):
        for built in self.sets.values():
            for code in built.files.values():
                self.assertNotIn("Babel", code)
                self.assertNotRegex(code, r"\beval\(|new Function\(")

    def test_deployment_values_stay_names_until_the_file_is_served(self):
        self.assertIn("__DIRECT_UPLOAD__", self.sets["onboard"].files["index.app.js"])
        self.assertIn("__AUTH_ON__", self.sets["hr"].files["hr.app.js"])

    def test_the_vendor_libraries_are_our_own_copies_and_heic2any_is_not_shipped(self):
        names = set(self.sets["onboard"].vendor)
        self.assertIn("react-18.3.1.production.min.js", names)
        self.assertIn("cropper-1.6.2.min.css", names)
        self.assertFalse([name for name in names if "heic" in name.lower()])

    def test_a_page_is_only_rebuilt_when_its_source_changed(self):
        built = self.sets["onboard"]
        mtime = (built.folder / "index.app.js").stat().st_mtime_ns
        assets.AssetSet("onboard", {"index": self.onboard.render_page}, dist=self.folder.name)
        self.assertEqual((built.folder / "index.app.js").stat().st_mtime_ns, mtime)
        assets.AssetSet("onboard", {"index": lambda: self.onboard.render_page() + "<!-- changed -->"}, dist=self.folder.name)
        self.assertNotEqual((built.folder / "index.app.js").stat().st_mtime_ns, mtime)

    def test_split_page_refuses_a_page_that_still_loads_from_another_site(self):
        with self.assertRaises(ValueError):
            assets.split_page("x", '<html><script src="https://evil.example/a.js"></script></html>')

    def test_jsx_inside_a_string_is_not_mistaken_for_page_markup(self):
        # the onboarding page builds a downloadable HTML document inside its JavaScript, with a <style> in it
        html = '<html><head><style>a{}</style></head><body><script type="text/babel">const t = `<style>b{}</style>`; const x = <p>{t}</p>;</script></body></html>'
        page, files = assets.split_page("t", html)
        self.assertIn("<style>b{}</style>", files["t.app.js"])
        self.assertEqual(files["t.css"], "a{}")
        self.assertNotIn("<style", page)


class PolicyTests(unittest.TestCase):

    def test_the_employee_policy_is_the_requested_one(self):
        with mock.patch.dict(os.environ, {"STORAGE_BACKEND": "dynamodb_s3", "S3_DOCUMENTS_BUCKET": DOCS, "AWS_REGION": REGION}):
            policy = security_headers.policy("onboarding")
        self.assertEqual(policy, (
            "default-src 'self'; script-src 'self'; "
            f"connect-src 'self' {BUCKET_ORIGIN}; "
            "img-src 'self' data: blob:; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"))

    def test_the_hr_policy_adds_the_bucket_to_images_and_frames_only(self):
        with mock.patch.dict(os.environ, {"STORAGE_BACKEND": "dynamodb_s3", "S3_DOCUMENTS_BUCKET": DOCS, "AWS_REGION": REGION}):
            policy = security_headers.policy("hr")
        self.assertIn("connect-src 'self';", policy)
        self.assertIn(f"img-src 'self' data: blob: {BUCKET_ORIGIN};", policy)
        self.assertIn(f"frame-src 'self' {BUCKET_ORIGIN};", policy)
        # the HR page frames its own document files, so only itself may frame (never another site)
        self.assertIn("frame-ancestors 'self'", policy)

    def test_the_employee_page_may_not_be_framed_at_all(self):
        self.assertIn("frame-ancestors 'none'", security_headers.policy("onboarding"))

    def test_never_unsafe_inline_or_eval_or_a_wildcard(self):
        with mock.patch.dict(os.environ, {"STORAGE_BACKEND": "dynamodb_s3", "S3_DOCUMENTS_BUCKET": DOCS, "AWS_REGION": REGION}):
            for portal in ("onboarding", "hr"):
                policy = security_headers.policy(portal)
                for bad in ("unsafe-inline", "unsafe-eval", " * ", "http:", "https:;"):
                    self.assertNotIn(bad, policy + " ")

    def test_a_local_run_names_no_bucket(self):
        with mock.patch.dict(os.environ, {"STORAGE_BACKEND": "local", "S3_DOCUMENTS_BUCKET": DOCS, "AWS_REGION": REGION}):
            self.assertNotIn("amazonaws", security_headers.policy("onboarding"))
            self.assertNotIn("amazonaws", security_headers.policy("hr"))


class ServerHeaderTests(AwsTestCase):
    """What the two running servers send."""

    def setUp(self):
        super().setUp()
        from starlette.testclient import TestClient

        patch = mock.patch.dict(os.environ, {"HR_USERS": json.dumps(HR_USERS), "SESSION_SECRET": HR_SECRET})
        patch.start()
        self.addCleanup(patch.stop)
        self.portal = TestClient(fresh("hr_onboarding.employee_portal", {}).app, base_url="https://testserver", follow_redirects=False)
        self.hr = TestClient(fresh("hr_onboarding.hr_portal", {}).app, base_url="https://testserver", follow_redirects=False)
        self.addCleanup(sys.modules.pop, "hr_onboarding.employee_portal", None)
        self.addCleanup(sys.modules.pop, "hr_onboarding.hr_portal", None)

    def assertHardened(self, reply, portal):
        for name in HEADERS:
            self.assertIn(name, reply.headers, f"{name} missing on {reply.request.url}")
        self.assertEqual(reply.headers["content-security-policy"], security_headers.policy(portal))
        self.assertEqual(reply.headers["x-content-type-options"], "nosniff")
        self.assertEqual(reply.headers["referrer-policy"], "no-referrer")
        self.assertIn("max-age=31536000", reply.headers["strict-transport-security"])

    def test_the_employee_portal_sends_them_on_every_kind_of_response(self):
        for path in ("/", "/health", "/nothing-here", "/assets/index.app.js", "/assets/vendor/react-18.3.1.production.min.js"):
            with self.subTest(path):
                self.assertHardened(self.portal.get(path), "onboarding")
        self.assertHardened(self.portal.post("/api/upload-urls", json={}), "onboarding")  # an error reply too

    def test_the_hr_portal_sends_them_on_every_kind_of_response(self):
        for path in ("/login", "/health", "/", "/api/employees", "/nothing-here", "/assets/login.css", "/assets/hr.app.js"):
            with self.subTest(path):
                self.assertHardened(self.hr.get(path), "hr")  # the sign-in redirect and the 401 carry them too

    def test_the_employee_policy_names_the_regional_bucket_address(self):
        csp = self.portal.get("/").headers["content-security-policy"]
        self.assertIn(f"connect-src 'self' {BUCKET_ORIGIN};", csp)

    def test_the_pages_are_served_and_work_without_any_cdn(self):
        page = self.portal.get("/").text
        self.assertIn("/assets/vendor/react-18.3.1.production.min.js", page)
        self.assertNotIn("unpkg", page)
        script = self.portal.get("/assets/index.app.js")
        self.assertEqual(script.status_code, 200)
        self.assertIn("text/javascript", script.headers["content-type"])
        self.assertIn("const DIRECT_UPLOAD = true;", script.text)  # the DynamoDB backend, swapped in when served
        self.assertNotIn("__DIRECT_UPLOAD__", script.text)

    def test_the_hr_script_is_open_but_the_data_is_not(self):
        self.assertEqual(self.hr.get("/assets/hr.app.js").status_code, 200)
        self.assertIn("const AUTH_ON = true;", self.hr.get("/assets/hr.app.js").text)
        self.assertEqual(self.hr.get("/api/employees").status_code, 401)

    def test_vendor_files_are_cached_for_a_year_and_unknown_files_are_404(self):
        reply = self.portal.get("/assets/vendor/cropper-1.6.2.min.js")
        self.assertIn("immutable", reply.headers["cache-control"])
        for path in ("/assets/vendor/nope.js", "/assets/vendor/..%2f..%2fstorage.py", "/assets/nope.js", "/assets/..%2fstorage.py"):
            self.assertEqual(self.portal.get(path).status_code, 404, path)

    def test_the_company_logo_is_served_as_an_image_and_both_portals_show_it(self):
        for client in (self.portal, self.hr):
            reply = client.get("/assets/vendor/intensity-logo-v1.png")
            self.assertEqual(reply.status_code, 200)
            self.assertEqual(reply.headers["content-type"], "image/png")
            self.assertTrue(reply.content.startswith(b"\x89PNG"))
        self.assertIn("intensity-logo-v1.png", self.portal.get("/assets/index.app.js").text)
        self.assertIn("intensity-logo-v1.png", self.hr.get("/assets/hr.app.js").text)
        self.assertIn("intensity-logo-v1.png", self.hr.get("/login").text)

    def test_the_pages_are_never_cached(self):
        self.assertEqual(self.portal.get("/").headers["cache-control"], "no-store")
        self.assertEqual(self.hr.get("/login").headers["cache-control"], "no-store")


if __name__ == "__main__":
    unittest.main()

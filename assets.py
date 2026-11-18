"""
Static files for the two pages, so a strict Content-Security-Policy (script-src 'self') can be used.

Each page is written as one Python string with inline <style>, inline <script> and JSX that the
browser used to compile with Babel loaded from a CDN. A strict policy forbids all three, so:

  - at build time (once: `python scripts/build_assets.py`, or automatically at start-up when the
    page changed and `mini-racer` is installed) the JSX is compiled to plain JavaScript by Babel
    running in V8, the inline <style> and <script> blocks become files, and the page's links to
    CDNs become links to our own copies in static/vendor/;
  - at run time nothing is compiled: the files are read from static/dist/<set>/ and served by the
    page's own server. The Docker image builds them in a first stage, so the Lambda image does
    not contain Babel or V8.

Values that depend on the running deployment (such as __DIRECT_UPLOAD__) stay as names in the
compiled JavaScript and are swapped when the file is served.
"""

import hashlib
import json
import re
from pathlib import Path

from starlette.responses import HTMLResponse, Response

BASE_DIR = Path(__file__).resolve().parent
VENDOR_DIR = BASE_DIR / "static" / "vendor"
DIST_DIR = BASE_DIR / "static" / "dist"
BABEL = BASE_DIR / "tools" / "babel-standalone-7.26.4.min.js"

# CDN address in the page -> our own copy (served at /assets/vendor/<file>)
VENDOR = {
    "https://unpkg.com/react@18.3.1/umd/react.production.min.js": "react-18.3.1.production.min.js",
    "https://unpkg.com/react-dom@18.3.1/umd/react-dom.production.min.js": "react-dom-18.3.1.production.min.js",
    "https://cdnjs.cloudflare.com/ajax/libs/cropperjs/1.6.2/cropper.min.js": "cropper-1.6.2.min.js",
    "https://cdnjs.cloudflare.com/ajax/libs/cropperjs/1.6.2/cropper.min.css": "cropper-1.6.2.min.css",
}

BABEL_SCRIPT = re.compile(r'<script type="text/babel"[^>]*>(.*?)</script>', re.S)
PLAIN_SCRIPT = re.compile(r"<script>(.*?)</script>", re.S)
STYLE = re.compile(r"<style[^>]*>(.*?)</style>", re.S)
BABEL_CDN = re.compile(r'<script src="https://unpkg\.com/@babel/standalone[^"]*"[^>]*></script>\n?')
FONT_LINK = re.compile(r'<link[^>]*(?:fonts\.googleapis\.com|fonts\.gstatic\.com)[^>]*>\n?')
EXTERNAL = re.compile(r'(?:src|href)="https?://')

MEDIA_TYPES = {".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8"}


class AssetsOutOfDate(RuntimeError):
    pass


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def compile_jsx(source):
    """Plain JavaScript for a JSX source, using Babel in V8 (needs the `mini-racer` package)."""

    try:
        from py_mini_racer import MiniRacer
    except ImportError as error:
        raise AssetsOutOfDate(
            "The page files in static/dist are missing or older than the code, and mini-racer is not "
            "installed to rebuild them. Run: pip install mini-racer && python scripts/build_assets.py"
        ) from error

    context = MiniRacer()
    context.eval(BABEL.read_text(encoding="utf-8"))
    context.eval("var __source = " + json.dumps(source) + ";")
    return context.eval('Babel.transform(__source, {presets: ["react"], sourceType: "script"}).code')


def split_page(name, html):
    """(page html with links, {file name: text}) for one assembled page.

    Pieces inside the JSX (a string that holds <style>, say) are safe: the JSX is taken out first.
    """

    files = {}
    jsx = []

    def take_jsx(match):
        jsx.append(match.group(1))
        return "\0JSX\0"

    html = BABEL_SCRIPT.sub(take_jsx, html)

    # Each plain script keeps its own place and its own file (one in the head must run before the body,
    # one at the end of the body needs the elements above it)
    def take_script(match):
        file = f"{name}.script{len(files)}.js"
        files[file] = match.group(1)
        return f'<script src="/assets/{file}?v={digest(match.group(1))}"></script>'

    html = PLAIN_SCRIPT.sub(take_script, html)
    css = STYLE.findall(html)
    html = STYLE.sub("\0CSS\0", html)

    html = BABEL_CDN.sub("", html)
    html = FONT_LINK.sub("", html)
    for cdn, local in VENDOR.items():
        html = html.replace(cdn, f"/assets/vendor/{local}")
    html = html.replace(' crossorigin>', '>')

    def link(placeholder, tag, text, suffix):
        nonlocal html
        if not text:
            html = html.replace(placeholder, "")
            return
        file = f"{name}.{suffix}"
        files[file] = text
        html = html.replace(placeholder, tag.format(file=file, v=digest(text)), 1).replace(placeholder, "")

    link("\0CSS\0", '<link rel="stylesheet" href="/assets/{file}?v={v}">', "\n".join(css), "css")
    link("\0JSX\0", '<script src="/assets/{file}?v={v}"></script>', compile_jsx("\n".join(jsx)) if jsx else "", "app.js")

    external = EXTERNAL.findall(html)
    if external:
        raise ValueError(f"Page {name} still loads something from another site: {external}")

    return html, files


class AssetSet:
    """The pages of one server: build or load them, then serve their files.

    `pages` maps a page name to a function that returns its assembled HTML. `tokens` (set by the
    server) returns {name in the JavaScript: text} to swap in when a script is served.
    """

    def __init__(self, set_name, pages, dist=DIST_DIR):
        self.folder = Path(dist) / set_name
        self.html, self.files = {}, {}
        self.tokens = lambda: {}
        manifest_path = self.folder / "manifest.json"

        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            manifest = {}

        rebuilt = False
        for name, render in pages.items():
            source = render()
            fresh = manifest.get(name) == digest(source) and (self.folder / f"{name}.html").is_file()

            if not fresh:
                page, files = split_page(name, source)
                self._write(name, page, files)
                manifest[name] = digest(source)
                rebuilt = True

            self._read(name)

        if rebuilt:
            manifest_path.write_text(json.dumps(manifest, indent=1), encoding="utf-8")

        self.vendor = {path.name: path for path in VENDOR_DIR.glob("*") if path.is_file()}

    def _write(self, name, page, files):
        self.folder.mkdir(parents=True, exist_ok=True)
        (self.folder / f"{name}.html").write_text(page, encoding="utf-8")
        for file, text in files.items():
            (self.folder / file).write_text(text, encoding="utf-8")

    def _read(self, name):
        self.html[name] = (self.folder / f"{name}.html").read_text(encoding="utf-8")
        for path in self.folder.glob(f"{name}.*"):
            if path.suffix in MEDIA_TYPES:
                self.files[path.name] = path.read_text(encoding="utf-8")

    def page(self, name, status_code=200, headers=None):
        return HTMLResponse(self.html[name], status_code=status_code, headers={"Cache-Control": "no-store", **(headers or {})})

    async def asset(self, request):
        file = request.path_params["file"]
        text = self.files.get(file)

        if text is None:
            return Response("Not found", status_code=404)

        for token, value in self.tokens().items():
            text = text.replace(token, value)

        return Response(text, media_type=MEDIA_TYPES[Path(file).suffix], headers={"Cache-Control": "no-store"})

    async def vendor_file(self, request):
        path = self.vendor.get(request.path_params["file"])

        if path is None:
            return Response("Not found", status_code=404)

        return Response(
            path.read_bytes(), media_type=MEDIA_TYPES[path.suffix],
            headers={"Cache-Control": "public, max-age=31536000, immutable"},  # the file name holds its version
        )

    def routes(self):
        from starlette.routing import Route

        return [
            Route("/assets/vendor/{file}", self.vendor_file),
            Route("/assets/{file}", self.asset),
        ]

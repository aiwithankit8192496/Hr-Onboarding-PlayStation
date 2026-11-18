"""
Security headers for both portals, set on every response by the app itself (so they hold wherever the
app runs, not only behind CloudFront).

The Content-Security-Policy is strict because the pages need nothing from other sites: scripts, styles
and the React/Cropper libraries are our own files (assets.py), so there is no 'unsafe-inline' and no
'unsafe-eval'. The only other address is the documents bucket, written as the page's own settings
give it (S3_DOCUMENTS_BUCKET, AWS_REGION):

  employee portal  connect-src: the bucket (the browser uploads each file there)
  HR portal        img-src and frame-src: the bucket (a document opens through a 5-minute link
                   that the app redirects to); it uploads nothing, so connect-src stays 'self'
"""

import os

HSTS = "max-age=31536000; includeSubDomains"


def documents_origin():
    """https://<bucket>.s3.<region>.amazonaws.com, or "" when the documents are not in S3 (local run)."""

    bucket, region = os.environ.get("S3_DOCUMENTS_BUCKET", "").strip(), os.environ.get("AWS_REGION", "").strip()
    backend = os.environ.get("STORAGE_BACKEND", "local").strip().lower() or "local"

    if backend == "local" or not bucket or not region:
        return ""

    return f"https://{bucket}.s3.{region}.amazonaws.com"


def policy(portal):
    """The Content-Security-Policy text for "onboarding" or "hr"."""

    bucket = documents_origin()

    if portal == "onboarding":
        directives = [
            "default-src 'self'",
            "script-src 'self'",
            f"connect-src 'self' {bucket}".strip(),
            "img-src 'self' data: blob:",
        ]
    else:
        directives = [
            "default-src 'self'",
            "script-src 'self'",
            "connect-src 'self'",
            f"img-src 'self' data: blob: {bucket}".strip(),
            f"frame-src 'self' {bucket}".strip(),
        ]

    # frame-ancestors: nobody may put the page in a frame. base-uri and form-action close two small gaps.
    directives += ["frame-ancestors 'none'", "base-uri 'self'", "form-action 'self'"]

    return "; ".join(directives)


class SecurityHeaders:
    """ASGI middleware: the same headers on every response, whatever the route (errors and redirects too)."""

    def __init__(self, app, portal):
        self.app = app
        self.portal = portal

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        headers = [
            (b"content-security-policy", policy(self.portal).encode()),
            (b"x-content-type-options", b"nosniff"),
            (b"referrer-policy", b"no-referrer"),
            (b"strict-transport-security", HSTS.encode()),
            (b"permissions-policy", b"camera=(self), microphone=(), geolocation=()"),
        ]
        names = {name for name, _ in headers}

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                kept = [(name, value) for name, value in message.get("headers", []) if name.lower() not in names]
                message = {**message, "headers": kept + headers}
            await send(message)

        await self.app(scope, receive, send_with_headers)

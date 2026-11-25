// CloudFront Function (runtime cloudfront-js-2.0), event type: viewer request.
// Attach it to the DEFAULT behaviour of the HR distribution only (setup guide, Step 18).
//
// 1. Only the office addresses may reach the HR portal; everyone else gets a plain 403 (and learns
//    nothing else, not even whether the portal is open).
// 2. From 00:00 to 06:59 India time the portal is closed: the Function answers by itself with
//    HTTP 503 and a small page. Lambda's reserved concurrency 0 (Step 19) is the hard switch behind it.
//    The night check is the same as in portal-viewer-request.js (keep the two in step).
//
// Edit OFFICE_IPS (one IPv4 address, or a range like "203.0.113.0/24"), then publish the function again.
// The documentation addresses below are examples, not real: with them nobody can get in.
// The HR sign-in itself is a login form and a signed cookie in the app (hr_login.py), not this Function.

var OFFICE_IPS = [
  "203.0.113.10",
  "198.51.100.0/24",
];

var CLOSED_HTML =
  '<!doctype html><html lang="en"><head><meta charset="utf-8">' +
  '<meta name="viewport" content="width=device-width, initial-scale=1"><meta name="robots" content="noindex">' +
  "<title>Portal closed</title>" +
  "<style>" +
  ":root{--bg:#f5f6f8;--card:#fff;--ink:#1a1d23;--muted:#5b6270;--line:#dfe3ea}" +
  "@media (prefers-color-scheme:dark){:root{--bg:#13151a;--card:#1c1f26;--ink:#eef0f4;--muted:#9aa2b1;--line:#2c313b}}" +
  "html,body{height:100%;margin:0}" +
  "body{display:flex;align-items:center;justify-content:center;padding:16px;background:var(--bg);color:var(--ink);" +
  'font:16px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}' +
  "main{max-width:420px;text-align:center;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:32px 24px}" +
  "h1{margin:0 0 8px;font-size:20px}p{margin:0;color:var(--muted)}" +
  "</style></head><body><main>" +
  "<h1>The portal is available 7 am &ndash; 12 am IST</h1>" +
  "<p>It is closed for now. Please come back during these hours.</p>" +
  "</main></body></html>";

// True from 00:00 to 06:59 in Asia/Kolkata (UTC+05:30, no daylight saving), for a time in
// milliseconds since 1970. 06:59:59 is still closed; 07:00:00 is open.
function isClosed(nowMs) {
  var ist = new Date(nowMs + 19800000);
  return ist.getUTCHours() < 7;
}

function toNumber(address) {
  var parts = address.split(".");
  if (parts.length !== 4) return null;
  var number = 0;
  for (var i = 0; i < 4; i++) {
    var part = parseInt(parts[i], 10);
    if (isNaN(part) || part < 0 || part > 255) return null;
    number = number * 256 + part;
  }
  return number;
}

function allowed(address) {
  var ip = toNumber(address);
  if (ip === null) return false; // IPv6 or garbage: not allowed (also turn IPv6 off on the distribution)
  for (var i = 0; i < OFFICE_IPS.length; i++) {
    var entry = OFFICE_IPS[i].split("/");
    var base = toNumber(entry[0]);
    var bits = entry.length > 1 ? parseInt(entry[1], 10) : 32;
    if (base === null || isNaN(bits) || bits < 0 || bits > 32) continue;
    var size = Math.pow(2, 32 - bits);
    if (Math.floor(ip / size) === Math.floor(base / size)) return true;
  }
  return false;
}

function closedResponse() {
  return {
    statusCode: 503,
    statusDescription: "Service Unavailable",
    headers: {
      "content-type": { value: "text/html; charset=utf-8" },
      "cache-control": { value: "no-store" },
      "retry-after": { value: "3600" },
    },
    body: CLOSED_HTML,
  };
}

function handler(event) {
  if (!allowed(event.viewer.ip)) {
    return {
      statusCode: 403,
      statusDescription: "Forbidden",
      headers: { "cache-control": { value: "no-store" } },
      body: "Forbidden",
    };
  }

  if (isClosed(Date.now())) {
    return closedResponse();
  }

  return event.request;
}

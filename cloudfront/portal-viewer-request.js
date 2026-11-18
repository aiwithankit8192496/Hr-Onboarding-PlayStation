// CloudFront Function (runtime cloudfront-js-2.0), event type: viewer request.
// Attach it to the DEFAULT behaviour of the EMPLOYEE distribution (setup guide, Step 18).
//
// The portal is closed from 00:00 to 06:59 India time, every day. In those hours this Function answers
// by itself with HTTP 503 and a small page, so the request never reaches Lambda. It is the friendly
// face of the night switch; Lambda's reserved concurrency 0 (Step 19) is the hard switch behind it.
// The same night check is in hr-viewer-request.js (keep the two in step).

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
  if (isClosed(Date.now())) {
    return closedResponse();
  }
  return event.request;
}

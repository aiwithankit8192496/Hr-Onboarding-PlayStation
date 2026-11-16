"""
Light / dark / default colour theme shared by both frontends (the onboarding portal in
react_onboard.py and the HR portal in hr_info.py).

Default follows the device setting (prefers-color-scheme); Light and Dark are the user's own
choice, kept in localStorage. The head script puts the result on <html data-theme="light|dark">
before the first paint, so there is no flash; each page defines its own dark colours under
:root[data-theme="dark"]. inject() adds the pieces to a page, like policies.inject().
"""

HEAD = r"""<script>
(function () {
  var KEY = "hr-theme", root = document.documentElement;
  var query = window.matchMedia("(prefers-color-scheme: dark)");
  function mode() {
    try { var saved = localStorage.getItem(KEY); } catch (e) {}
    return saved === "light" || saved === "dark" ? saved : "system";
  }
  function apply() {
    var chosen = mode();
    root.setAttribute("data-theme-mode", chosen);
    root.setAttribute("data-theme", chosen === "system" ? (query.matches ? "dark" : "light") : chosen);
  }
  window.getThemeMode = mode;
  window.setThemeMode = function (chosen) {
    try { chosen === "system" ? localStorage.removeItem(KEY) : localStorage.setItem(KEY, chosen); } catch (e) {}
    apply();
  };
  if (query.addEventListener) query.addEventListener("change", apply); else query.addListener(apply);
  apply();
})();
</script>"""

CSS = r"""
/* ---------- Theme toggle (Default / Light / Dark) ---------- */
:root[data-theme="dark"] { color-scheme: dark; }
:root[data-theme="light"] { color-scheme: light; }
.theme-toggle { display: inline-flex; gap: 2px; padding: 3px; flex: none; border-radius: 10px; border: 1px solid var(--line-strong, var(--field-line, #cbd5e1)); background: var(--surface, var(--card, #fff)); }
.theme-toggle button { border: 0; background: transparent; color: var(--muted); font: inherit; font-size: 12.5px; font-weight: 600; padding: 4px 10px; border-radius: 7px; cursor: pointer; white-space: nowrap; }
.theme-toggle button:hover { color: var(--ink); }
.theme-toggle button.on { background: var(--brand, var(--primary, #4f46e5)); color: #fff; }
.theme-toggle button:focus-visible { outline: 2px solid var(--brand, var(--primary, #4f46e5)); outline-offset: 1px; }
.theme-toggle.on-dark { background: rgba(255, 255, 255, 0.12); border-color: rgba(255, 255, 255, 0.35); }
.theme-toggle.on-dark button { color: rgba(255, 255, 255, 0.85); }
.theme-toggle.on-dark button:hover { color: #fff; }
.theme-toggle.on-dark button.on { background: #fff; color: #142d59; }
@media (max-width: 640px) { .theme-toggle button { padding: 4px 7px; font-size: 12px; } }
"""

SCRIPT = r"""
const THEME_CHOICES = [
  ["system", "Default", "Follow your device's light or dark setting"],
  ["light", "Light", "Always light"],
  ["dark", "Dark", "Always dark"],
];

function ThemeToggle({ onDark = false }) {
  const [mode, setMode] = useState(window.getThemeMode());

  function choose(value) {
    window.setThemeMode(value);
    setMode(value);
  }

  return (
    <div className={`theme-toggle${onDark ? " on-dark" : ""}`} role="group" aria-label="Colour theme">
      {THEME_CHOICES.map(([value, label, hint]) => (
        <button
          key={value}
          type="button"
          className={mode === value ? "on" : ""}
          aria-pressed={mode === value}
          title={hint}
          onClick={() => choose(value)}
        >
          {label}
        </button>
      ))}
    </div>
  );
}
"""


def inject(page):
    """Add the theme script, styles and toggle component to a frontend page."""

    return (
        page.replace("<!-- __THEME_HEAD__ -->", HEAD)
        .replace("/* __THEME_CSS__ */", CSS)
        .replace("// __THEME_SCRIPT__", SCRIPT)
    )

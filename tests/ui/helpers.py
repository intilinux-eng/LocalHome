"""What the browser tests share - see conftest.py for the server and browser."""

# The two zones the test server is started with: an apostrophe, and a
# name that would run a script if any page put it into HTML unescaped.
ZONE_A = "Camera dell'ospite"
ZONE_B = 'Bagno <img src=x onerror=alert(1)> "2"'


class Opened:
    """A page plus the script errors it has logged so far."""

    def __init__(self, page):
        self.page = page
        self.errors = []
        page.on("pageerror", lambda e: self.errors.append(str(e)))
        page.on("console", lambda m: self.errors.append(m.text) if m.type == "error" else None)


def eventually(page, predicate, timeout_ms=10000, step_ms=250):
    """Waits for predicate() to be true, giving the page's polls time to run."""
    waited = 0
    while not predicate():
        if waited >= timeout_ms:
            return False
        page.wait_for_timeout(step_ms)
        waited += step_ms
    return True


UNTRANSLATED_JS = r"""() => {
  const text = document.body.innerText + ' ' + [...document.querySelectorAll('[title]')].map(e => e.title).join(' ');
  return [...new Set(text.match(/\b(app|climate|climate_admin|sensors|fields|settings|clock|errors|vmc_presets)\.[a-z_0-9]+/g) || [])];
}"""

OVERFLOW_JS = "document.documentElement.scrollWidth - document.documentElement.clientWidth"

"""
One small driver interface over a real browser (Playwright / Chromium).

The task never changes across surfaces; only this driver does. resolve() is the
interesting part: it finds a field by tiers — label first, then structure — and
records which tier fired, because tier-1 quietly failing over to tier-2 across
runs is the earliest signal a screen changed.
"""
from playwright.sync_api import sync_playwright

# Screen signatures: labels that must ALL be present, plus optional negatives.
# transfer_confirm carries a negative ("Account flagged") so it can't collide
# with the flagged dialog, which shares the "Confirm Transfer" header.
SCREENS = {
    "member_search":   {"must": ["Member Search", "Member ID", "Search"]},
    "member_summary":  {"must": ["Member Summary", "Transfer Funds"]},
    "transfer_form":   {"must": ["Internal Transfer", "From Account", "Amount", "Continue"]},
    "transfer_confirm": {"must": ["Confirm Transfer", "Submit Transfer"],
                         "must_not": ["flagged"]},
    "flagged":         {"must": ["Confirm Transfer", "flagged"]},
    "receipt":         {"must": ["Transfer Complete", "Confirmation"]},
    "recent_activity": {"must": ["Recent Activity"]},
}


class Driver:
    def __init__(self, base, headed=False, slow=0):
        self.base = base
        self._pw = sync_playwright().start()
        self.browser = self._pw.chromium.launch(headless=not headed, slow_mo=slow)
        self.page = self.browser.new_page(viewport={"width": 1100, "height": 720})

    # ---- looking ----
    def goto(self, path):
        self.page.goto(self.base + path, wait_until="domcontentloaded")

    def body_text(self):
        return self.page.inner_text("body")

    def identify(self):
        text = self.body_text()
        for name, sig in SCREENS.items():
            if all(m in text for m in sig["must"]) and \
               not any(n in text for n in sig.get("must_not", [])):
                return name
        return "UNKNOWN"

    # ---- locating (tiered) ----
    def resolve(self, label, kind="input"):
        """Return (locator, tier_label). tier 1 = by label; tier 2 = structural."""
        # tier 1: the control immediately following the label text
        t1 = self.page.locator(
            f'xpath=//*[normalize-space(text())="{label}"]/following::{kind}[1]')
        if t1.count() == 1:
            return t1, f"tier 1 · label '{label}'"
        # tier 2: fall back to the first control of that kind in the form
        t2 = self.page.locator(f"{kind}").first
        return t2, f"tier 2 · structural <{kind}>"

    # ---- acting (each verified by readback) ----
    def read(self, locator):
        return locator.input_value()

    def set(self, locator, value, select=False):
        if select:
            locator.select_option(value=value)
        else:
            locator.fill(value)
        return self.read(locator)                 # readback: the only receipt we get

    def click(self, name, kind="button"):
        sel = (f'xpath=//button[normalize-space()="{name}"]' if kind == "button"
               else f'xpath=//a[contains(normalize-space(),"{name}")]')
        with self.page.expect_navigation(wait_until="domcontentloaded"):
            self.page.click(sel)

    def close(self):
        self.browser.close()
        self._pw.stop()

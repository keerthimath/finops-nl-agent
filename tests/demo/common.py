"""Shared helpers for the security demo (tests/demo). Not part of the unit test suite.

Credentials come from the git-ignored .env (DEMO_TEAM_<A|B>_UPN / _PASSWORD / _TOTP_SECRET); nothing here prints them.
"""
from __future__ import annotations

import re
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = ROOT / ".env"
OUT_DIR = ROOT / "docs" / "demo"

# Anything that could identify the tenant, subscription or billing account is masked on the page itself before a
# screenshot or video frame is taken (see MASK_SCRIPT).
MASK_PATTERNS = [
    (r"[A-Za-z0-9._%+-]+#EXT#@[A-Za-z0-9.-]+", "guest-user#EXT#@contoso.onmicrosoft.com"),   # guest UPNs embed an email
    (r"[A-Za-z0-9._%+-]+@(?:outlook|hotmail|live|gmail|yahoo|icloud)\.[a-z.]+", "user@example.com"),
    (r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"),
    (r"[A-Za-z0-9-]+\.onmicrosoft\.com", "contoso.onmicrosoft.com"),
    (r"/subscriptions/[^\s'\"/|]+", "/subscriptions/<masked>"),
    (r"(?i)(billing ?account(?: ?name| ?id)?\s*[:=|]\s*)[^\n|]+", r"\1<masked>"),
]


def load_env() -> dict:
    from dotenv import dotenv_values

    return dotenv_values(ENV_FILE)


def mask(text: str) -> str:
    for pattern, repl in MASK_PATTERNS:
        text = re.sub(pattern, repl, text)
    return text


MASK_SCRIPT = """
(() => {
  const rules = %s;
  const res = rules.map(([p, r]) => [new RegExp(p.replace(/^\\(\\?i\\)/, ''), p.startsWith('(?i)') ? 'gi' : 'g'), r.replace(/\\\\1/g, '$1')]);
  const fix = (s) => res.reduce((acc, [re, r]) => acc.replace(re, r), s);
  const walk = (node) => {
    if (node.nodeType === Node.TEXT_NODE) { const v = fix(node.nodeValue); if (v !== node.nodeValue) node.nodeValue = v; return; }
    if (node.nodeType !== Node.ELEMENT_NODE) return;
    if (node.tagName === 'INPUT' || node.tagName === 'TEXTAREA') return;   // never rewrite what the user is typing
    for (const c of node.childNodes) walk(c);
  };
  const start = () => { walk(document.body); new MutationObserver(ms => ms.forEach(m => {
      if (m.type === 'characterData') walk(m.target); m.addedNodes.forEach(walk); }))
    .observe(document.body, {childList: true, subtree: true, characterData: true}); };
  if (document.body) start(); else document.addEventListener('DOMContentLoaded', start);
})();
""" % __import__("json").dumps(MASK_PATTERNS)


def team(env: dict, t: str) -> dict:
    t = t.upper()
    return {"name": f"Team {t}", "upn": env[f"DEMO_TEAM_{t}_UPN"], "password": env[f"DEMO_TEAM_{t}_PASSWORD"],
            "totp": env.get(f"DEMO_TEAM_{t}_TOTP_SECRET") or ""}


def totp_code(secret: str) -> str:
    import pyotp

    totp = pyotp.TOTP(secret.replace(" ", ""))
    if totp.interval - (int(time.time()) % totp.interval) < 5:  # don't send a code that expires in transit
        time.sleep(6)
    return totp.now()


def sign_in(page, user: dict, timeout: int = 60_000) -> None:
    """Complete the Entra sign-in page (UPN, password, TOTP, 'Stay signed in?') wherever the app redirected to."""
    page.wait_for_selector("input[type=email]", timeout=timeout)
    page.fill("input[type=email]", user["upn"])
    page.click("input[type=submit]")
    page.wait_for_selector("input[type=password]", timeout=timeout)
    page.fill("input[type=password]", user["password"])
    page.click("input[type=submit]")
    deadline = time.time() + timeout / 1000
    while time.time() < deadline and "login.microsoftonline.com" in page.url:
        if page.locator("input[name=otc]").is_visible():
            if not user["totp"]:
                raise RuntimeError(f"{user['name']}: MFA code requested but no TOTP secret; run register_mfa.py first")
            page.fill("input[name=otc]", totp_code(user["totp"]))
            page.click("input[type=submit]")
        elif page.get_by_text("Stay signed in?").is_visible():
            page.click("#idBtn_Back")  # "No"
        elif page.get_by_text("Let's keep your account secure").is_visible():
            raise RuntimeError(f"{user['name']}: MFA not registered yet; run register_mfa.py first")
        time.sleep(1)

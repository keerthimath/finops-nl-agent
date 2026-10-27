"""One-time setup: register a TOTP authenticator for each demo user, so the demo can sign in unattended.

The tenant uses security defaults (MFA registration is forced at first sign-in; there's no per-user exclusion without
Conditional Access). This walks the registration as the user would, chooses "a different authentication app", reads
the secret key from "Can't scan the QR code?", and saves it to .env as DEMO_TEAM_<X>_TOTP_SECRET. Nothing is printed.

    .venv/bin/python tests/demo/register_mfa.py A B
"""
from __future__ import annotations

import re
import sys
import time

from common import ENV_FILE, load_env, team, totp_code


def save_secret(t: str, secret: str) -> None:
    lines = ENV_FILE.read_text().splitlines()
    key = f"DEMO_TEAM_{t}_TOTP_SECRET="
    lines = [key + secret if line.startswith(key) else line for line in lines]
    ENV_FILE.write_text("\n".join(lines) + "\n")


def register(t: str) -> None:
    from playwright.sync_api import sync_playwright

    user = team(load_env(), t)
    if user["totp"]:
        print(f"{user['name']}: already registered (DEMO_TEAM_{t}_TOTP_SECRET is set)")
        return
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.goto("https://mysignins.microsoft.com/security-info")
        page.fill("input[type=email]", user["upn"])
        page.click("input[type=submit]")
        page.wait_for_selector("input[type=password]")
        page.fill("input[type=password]", user["password"])
        page.click("input[type=submit]")
        page.get_by_text("Let's keep your account secure").wait_for(timeout=60_000)
        page.get_by_role("button", name="Next").click()
        page.get_by_text("Set up a different authentication app").click()
        page.get_by_text("Set up your account in app").wait_for()
        page.get_by_role("button", name="Next").click()
        page.get_by_text("Can't scan the QR code?").click()
        time.sleep(2)
        body = page.inner_text("body")
        match = re.search(r"Secret key:?\s*([A-Za-z2-7 ]{16,})", body)
        if not match:
            raise RuntimeError(f"{user['name']}: secret key not found on the page")
        secret = match.group(1).replace(" ", "").strip()
        save_secret(t, secret)  # save first, so a failure below doesn't lose it
        page.get_by_role("button", name="Next").click()
        code_box = page.locator("input:visible").first
        code_box.wait_for()
        code_box.fill(totp_code(secret))
        page.get_by_role("button", name="Next").click()
        page.wait_for_timeout(5000)
        done = page.get_by_role("button", name=re.compile("Done|Next"))
        if done.count():
            done.first.click()
            page.wait_for_timeout(3000)
        browser.close()
    print(f"{user['name']}: authenticator registered; secret saved to .env")


if __name__ == "__main__":
    for arg in sys.argv[1:] or ["A", "B"]:
        register(arg.upper())

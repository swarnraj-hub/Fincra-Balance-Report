#!/usr/bin/env python3

import argparse
import asyncio
import csv
import os
import pyotp
import requests
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, parse_qs, urlencode
from playwright.async_api import async_playwright

# ---------------------------------------------------------------------------
# ENV LOADER
# ---------------------------------------------------------------------------
_ENV_FILE = Path(__file__).parent / ".env"
if _ENV_FILE.exists():
    for line in _ENV_FILE.read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

# ---------------------------------------------------------------------------
# DATE PARSER
# ---------------------------------------------------------------------------
def parse_date(d):
    for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(d, fmt).strftime("%Y-%m-%d")
        except:
            pass
    return datetime.fromisoformat(d).strftime("%Y-%m-%d")

parser = argparse.ArgumentParser()
parser.add_argument("--start_date", required=True)
parser.add_argument("--end_date", required=False)
args = parser.parse_args()

START_DATE = parse_date(args.start_date)
END_DATE = parse_date(args.end_date) if args.end_date else datetime.now().strftime("%Y-%m-%d")

START_DT = datetime.strptime(START_DATE, "%Y-%m-%d")
END_DT = datetime.strptime(END_DATE, "%Y-%m-%d")

print(f"[*] Date range: {START_DATE} -> {END_DATE}")

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
USERNAME = os.environ.get("FINCRA_USERNAME")
PASSWORD = os.environ.get("FINCRA_PASSWORD")
TOTP_SECRET = os.environ.get("FINCRA_TOTP_SECRET")
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN")
SLACK_USER_ID = os.environ.get("SLACK_USER_ID")

DOWNLOAD_DIR = Path("downloads")
LOGIN_URL = "https://app.fincra.com/auth/login"

def otp():
    return pyotp.TOTP(TOTP_SECRET).now()

def file_date(d):
    return d.strftime("%d%m%Y")

PAYIN_FILE = f"FINCRA_PAYIN_{file_date(START_DT)}_to_{file_date(END_DT)}.csv"

# ---------------------------------------------------------------------------
# MODAL FIX (IMPORTANT)
# ---------------------------------------------------------------------------
async def _dismiss_modal(page):
    await page.evaluate("""
        () => document.querySelectorAll('.ReactModal__Overlay').forEach(e => e.remove())
    """)

    for sel in [
        "text=Remind Me Later",
        "text=No thanks",
        "text=Dismiss",
        "text=Skip",
        "[aria-label='Close']"
    ]:
        try:
            loc = page.locator(sel)
            if await loc.count() > 0:
                await loc.first.click(timeout=2000)
        except:
            pass

    try:
        await page.keyboard.press("Escape")
    except:
        pass

# ---------------------------------------------------------------------------
# LOGIN
# ---------------------------------------------------------------------------
async def login(page):
    await page.goto(LOGIN_URL)
    await page.fill('input[type="email"]', USERNAME)
    await page.fill('input[type="password"]', PASSWORD)
    await page.click('button[type="submit"]')
    await page.wait_for_timeout(3000)

    if await page.locator("input").count() >= 6:
        code = otp()
        inputs = page.locator("input")
        for i, c in enumerate(code):
            await inputs.nth(i).fill(c)
        await page.keyboard.press("Enter")
        await page.wait_for_timeout(3000)

# ---------------------------------------------------------------------------
# DATE PICKER (YOUR LOGIC FIXED)
# ---------------------------------------------------------------------------
async def _set_date_range(page):
    await page.click("text=Select Date")
    await page.wait_for_timeout(1000)

    # FROM
    await page.evaluate(f"""
        () => {{
            const cal = document.querySelector('.rdrCalendarWrapper') || document;
            const el = [...cal.querySelectorAll('button, td, span, div')]
                .find(e => (e.textContent || '').trim() === '{START_DT.day}');
            if (el) el.click();
        }}
    """)

    await page.wait_for_timeout(500)

    # TO
    await page.evaluate(f"""
        () => {{
            const cal = document.querySelector('.rdrCalendarWrapper') || document;
            const el = [...cal.querySelectorAll('button, td, span, div')]
                .find(e => (e.textContent || '').trim() === '{END_DT.day}');
            if (el) el.click();
        }}
    """)

    await page.wait_for_timeout(500)
    await page.keyboard.press("Escape")

# ---------------------------------------------------------------------------
# EXPORT PAYINS (FIXED)
# ---------------------------------------------------------------------------
async def export_payins(page, context):
    print(f"[payin] Exporting {START_DATE} -> {END_DATE}")

    await page.goto("https://app.fincra.com/payins")
    await page.wait_for_timeout(3000)

    await _dismiss_modal(page)

    # FIXED CLICK (fallback safe)
    try:
        await page.click("text=Show Filters", timeout=5000)
    except:
        await page.evaluate("""
            () => {
                const el = [...document.querySelectorAll('*')]
                    .find(e => (e.textContent || '').trim() === 'Show Filters');
                if (el) el.click();
            }
        """)

    await page.wait_for_timeout(1500)
    await _dismiss_modal(page)

    await _set_date_range(page)

    # Capture API
    api = None

    async def capture(req):
        nonlocal api
        if "payin" in req.url.lower():
            api = req.url

    page.on("request", capture)

    await page.click("text=Search")
    await page.wait_for_timeout(5000)

    page.remove_listener("request", capture)

    if not api:
        raise Exception("API not captured")

    print("[api]", api)

    parsed = urlparse(api)
    params = parse_qs(parsed.query)
    base = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"

    rows = []
    p = 1

    while True:
        params["page"] = [str(p)]
        url = f"{base}?{urlencode({k:v[0] for k,v in params.items()})}"

        res = await context.request.get(url)
        data = await res.json()

        r = data.get("data", {}).get("results", [])

        if not r:
            break

        rows.extend(r)

        if len(r) < 100:
            break

        p += 1

    DOWNLOAD_DIR.mkdir(exist_ok=True)
    path = DOWNLOAD_DIR / PAYIN_FILE

    keys = set()
    for r in rows:
        keys.update(r.keys())

    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(keys))
        w.writeheader()
        w.writerows(rows)

    print("[done]", path)
    return path

# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()

        try:
            await login(page)
            file = await export_payins(page, context)

            print("[+] SUCCESS:", file)

        finally:
            await browser.close()

if __name__ == "__main__":
    asyncio.run(main())

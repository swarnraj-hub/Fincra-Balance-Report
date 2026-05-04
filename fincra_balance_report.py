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
# Load .env
# ---------------------------------------------------------------------------
_ENV_FILE = Path(__file__).parent / ".env"
if _ENV_FILE.exists():
    for _line in _ENV_FILE.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

# ---------------------------------------------------------------------------
# Date parser (ROBUST)
# ---------------------------------------------------------------------------
def parse_date(date_str: str) -> str:
    if not date_str:
        raise ValueError("Empty date provided")

    for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(date_str, fmt).strftime("%Y-%m-%d")
        except:
            pass

    try:
        return datetime.fromisoformat(date_str.replace("Z", "")).strftime("%Y-%m-%d")
    except:
        pass

    raise ValueError(f"Invalid date format: {date_str}")

# ---------------------------------------------------------------------------
# Arguments
# ---------------------------------------------------------------------------
_parser = argparse.ArgumentParser()
_parser.add_argument("--start_date", required=True)
_parser.add_argument("--end_date", required=False)
_args = _parser.parse_args()

START_DATE = parse_date(_args.start_date)
END_DATE = parse_date(_args.end_date) if _args.end_date else datetime.now().strftime("%Y-%m-%d")

START_DT = datetime.strptime(START_DATE, "%Y-%m-%d")
END_DT   = datetime.strptime(END_DATE, "%Y-%m-%d")

if START_DT > END_DT:
    raise SystemExit("ERROR: start_date cannot be after end_date")

print(f"[*] Date range: {START_DATE} -> {END_DATE}")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
USERNAME = os.environ.get("FINCRA_USERNAME", "")
PASSWORD = os.environ.get("FINCRA_PASSWORD", "")
TOTP_SECRET = os.environ.get("FINCRA_TOTP_SECRET", "")
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
SLACK_USER_ID = os.environ.get("SLACK_USER_ID", "")

def to_file_date(d):
    return d.strftime("%d%m%Y")

PAYIN_FILENAME = f"FINCRA_PAYIN_{to_file_date(START_DT)}_to_{to_file_date(END_DT)}.csv"
DOWNLOAD_DIR = Path("downloads")
LOGIN_URL = "https://app.fincra.com/auth/login"

def get_otp():
    return pyotp.TOTP(TOTP_SECRET).now()

# ---------------------------------------------------------------------------
# Slack
# ---------------------------------------------------------------------------
def notify_slack(msg):
    if not SLACK_BOT_TOKEN or not SLACK_USER_ID:
        return
    requests.post(
        "https://slack.com/api/chat.postMessage",
        json={"channel": SLACK_USER_ID, "text": msg},
        headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"}
    )

# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------
async def do_login(page):
    await page.goto(LOGIN_URL)
    await page.fill('input[type="email"]', USERNAME)
    await page.fill('input[type="password"]', PASSWORD)
    await page.click('button[type="submit"]')
    await page.wait_for_timeout(3000)

    if await page.locator('input').count() >= 6:
        code = get_otp()
        inputs = page.locator('input')
        for i, d in enumerate(code):
            await inputs.nth(i).fill(d)
        await page.keyboard.press("Enter")
        await page.wait_for_timeout(3000)

# ---------------------------------------------------------------------------
# Calendar helpers
# ---------------------------------------------------------------------------
async def _calendar_nav_to(page, month, year):
    for _ in range(24):
        label = await page.locator('.rdrMonthAndYearPickers select').all_text_contents()
        if str(year) in "".join(label):
            break
        await page.click('.rdrNextButton')

async def _click_day(page, day):
    await page.click(f"text=\"{day}\"")

async def _set_date_range(page):
    await page.click("text=Select Date")
    await page.wait_for_timeout(1000)

    await _calendar_nav_to(page, START_DT.month, START_DT.year)
    await _click_day(page, START_DT.day)

    await _calendar_nav_to(page, END_DT.month, END_DT.year)
    await _click_day(page, END_DT.day)

    await page.keyboard.press("Escape")

# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------
async def export_payins(page, context):
    await page.goto("https://app.fincra.com/payins")
    await page.wait_for_timeout(3000)

    await page.click("text=Show Filters")
    await _set_date_range(page)

    api_url = None

    async def capture(req):
        nonlocal api_url
        if "payin" in req.url:
            api_url = req.url

    page.on("request", capture)

    await page.click("text=Search")
    await page.wait_for_timeout(5000)

    if not api_url:
        raise Exception("API not captured")

    parsed = urlparse(api_url)
    params = parse_qs(parsed.query)
    base = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"

    all_rows = []
    page_num = 1

    while True:
        params["page"] = [str(page_num)]
        url = f"{base}?{urlencode({k:v[0] for k,v in params.items()})}"

        resp = await context.request.get(url)
        data = await resp.json()

        rows = data.get("data", {}).get("results", [])
        if not rows:
            break

        all_rows.extend(rows)

        if len(rows) < 100:
            break

        page_num += 1

    DOWNLOAD_DIR.mkdir(exist_ok=True)
    path = DOWNLOAD_DIR / PAYIN_FILENAME

    keys = set()
    for r in all_rows:
        keys.update(r.keys())

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(keys))
        writer.writeheader()
        writer.writerows(all_rows)

    return path

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()

        try:
            await do_login(page)
            file = await export_payins(page, context)

            notify_slack(f"Done: {file.name}")
            print(f"[+] Done: {file}")

        finally:
            await browser.close()

if __name__ == "__main__":
    asyncio.run(main())

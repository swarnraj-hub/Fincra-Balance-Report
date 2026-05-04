#!/usr/bin/env python3
"""
Fincra Balance Report (Pay-In Export)

Date logic:
  - From Date : passed via --start_date arg (from Google Sheets via n8n)
  - To Date   : passed via --end_date arg, or today if omitted

Usage:
    python fincra_balance_report.py --start_date 2026-04-01 --end_date 2026-04-30
"""

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
    """Convert multiple formats to YYYY-MM-DD."""
    if not date_str:
        raise ValueError("Empty date provided")
    try:
        return datetime.strptime(date_str, "%Y-%m-%d").strftime("%Y-%m-%d")
    except Exception:
        pass
    try:
        return datetime.fromisoformat(date_str.replace("Z", "")).strftime("%Y-%m-%d")
    except Exception:
        pass
    try:
        return datetime.strptime(date_str, "%d/%m/%Y").strftime("%Y-%m-%d")
    except Exception:
        pass
    raise ValueError(f"Invalid date format: {date_str}")


# ---------------------------------------------------------------------------
# Arguments
# ---------------------------------------------------------------------------
_parser = argparse.ArgumentParser()
_parser.add_argument("--start_date", type=str, required=True)
_parser.add_argument("--end_date",   type=str, required=False)
_args = _parser.parse_args()

try:
    START_DATE = parse_date(_args.start_date)
    END_DATE   = parse_date(_args.end_date) if _args.end_date else datetime.now().strftime("%Y-%m-%d")
except ValueError as e:
    raise SystemExit(f"ERROR: {e}")

START_DT = datetime.strptime(START_DATE, "%Y-%m-%d")
END_DT   = datetime.strptime(END_DATE,   "%Y-%m-%d")

if START_DT > END_DT:
    raise SystemExit("ERROR: start_date cannot be after end_date")

print(f"[*] Date range: {START_DATE} -> {END_DATE}")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
USERNAME        = os.environ.get("FINCRA_USERNAME", "")
PASSWORD        = os.environ.get("FINCRA_PASSWORD", "")
TOTP_SECRET     = os.environ.get("FINCRA_TOTP_SECRET", "")
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
SLACK_USER_ID   = os.environ.get("SLACK_USER_ID", "")


def to_file_date(d: datetime) -> str:
    return d.strftime("%d%m%Y")


PAYIN_FILENAME = f"FINCRA_PAYIN_{to_file_date(START_DT)}_to_{to_file_date(END_DT)}.csv"
DOWNLOAD_DIR   = Path("downloads")
LOGIN_URL      = "https://app.fincra.com/auth/login"


def get_otp() -> str:
    return pyotp.TOTP(TOTP_SECRET).now()


async def ss(page, name: str) -> None:
    await page.screenshot(path=f"fincra_payin_{name}.png", full_page=False)
    print(f"  [screenshot] fincra_payin_{name}.png")


# ---------------------------------------------------------------------------
# Slack
# ---------------------------------------------------------------------------
def notify_slack(message: str, color: str = "good") -> None:
    if not SLACK_BOT_TOKEN or not SLACK_USER_ID:
        return
    headers = {"Authorization": f"Bearer {SLACK_BOT_TOKEN}", "Content-Type": "application/json"}
    channel_id = SLACK_USER_ID
    if SLACK_USER_ID.startswith("U"):
        try:
            r = requests.post(
                "https://slack.com/api/conversations.open",
                json={"users": SLACK_USER_ID}, headers=headers, timeout=10,
            )
            if r.json().get("ok"):
                channel_id = r.json()["channel"]["id"]
        except Exception:
            pass
    icon = {"good": ":white_check_mark:", "warning": ":warning:", "danger": ":x:"}.get(color, "")
    try:
        requests.post(
            "https://slack.com/api/chat.postMessage",
            json={"channel": channel_id, "text": f"{icon} {message}"},
            headers=headers, timeout=10,
        )
        print("[slack] DM sent.")
    except Exception as e:
        print(f"[slack] Failed: {e}")


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------
async def do_login(page) -> None:
    print("[login] Navigating to login page ...")
    await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30_000)
    await page.wait_for_timeout(2_000)

    await page.locator('input[type="email"], input[name="email"]').first.fill(USERNAME)
    await page.locator('input[type="password"]').first.fill(PASSWORD)
    await page.locator('button[type="submit"]').first.click()
    await page.wait_for_timeout(3_000)

    if "twofa" in page.url or await page.locator('input').count() >= 6:
        print("[login] 2FA detected ...")
        for attempt in range(1, 4):
            if "dashboard" in page.url or "payins" in page.url:
                break
            try:
                await page.wait_for_function(
                    "document.querySelectorAll('input').length >= 6", timeout=10_000)
            except Exception:
                if "dashboard" in page.url:
                    break
            code = get_otp()
            print(f"[login] OTP attempt {attempt}: {code}")
            inputs = page.locator('input')
            for i, digit in enumerate(code):
                try:
                    loc = inputs.nth(i)
                    await loc.click(timeout=5_000)
                    await page.wait_for_timeout(100)
                    el = await loc.element_handle(timeout=5_000)
                    await page.evaluate(
                        """([el, val]) => {
                            el.focus();
                            const setter = Object.getOwnPropertyDescriptor(
                                HTMLInputElement.prototype, 'value').set;
                            setter.call(el, val);
                            el.dispatchEvent(new Event('input',  {bubbles: true}));
                            el.dispatchEvent(new Event('change', {bubbles: true}));
                            el.dispatchEvent(new KeyboardEvent('keyup', {key: val, bubbles: true}));
                        }""", [el, digit])
                except Exception:
                    break
            await page.keyboard.press("Enter")
            await page.wait_for_timeout(3_000)
            if "dashboard" in page.url:
                break
            if attempt < 3:
                print("[login] OTP not accepted — waiting for next window ...")
                await page.wait_for_timeout(15_000)

    await _dismiss_survey(page)
    print(f"[login] Done. URL: {page.url}")


async def _dismiss_survey(page) -> None:
    await page.wait_for_timeout(1_500)
    for selector in [
        'button:has-text("Remind Me Later")', 'button:has-text("Remind me later")',
        'button:has-text("No thanks")',        'button:has-text("Dismiss")',
        'button:has-text("Skip")',             '[aria-label="Close"]',
    ]:
        try:
            loc = page.locator(selector)
            if await loc.count() > 0:
                await loc.first.click(timeout=3_000)
                await page.wait_for_timeout(600)
                break
        except Exception:
            pass
    if await page.locator('.ReactModal__Overlay').count() > 0:
        await page.keyboard.press('Escape')
        await page.wait_for_timeout(600)


# ---------------------------------------------------------------------------
# TODO: Add export_payins(), calendar helpers here
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
async def main() -> None:
    DOWNLOAD_DIR.mkdir(exist_ok=True)
    print("=" * 55)
    print(f"[*] Fincra Pay-In Export")
    print(f"[*] From : {START_DATE}")
    print(f"[*] To   : {END_DATE}")
    print(f"[*] File : {PAYIN_FILENAME}")
    print("=" * 55)

    IS_CI = os.environ.get("CI", "false").lower() == "true"

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=IS_CI, slow_mo=0 if IS_CI else 80)
        context = await browser.new_context(viewport={"width": 1440, "height": 900})
        page    = await context.new_page()
        try:
            await do_login(page)
            dest = await export_payins(page, context)

            notify_slack(
                f":white_check_mark: *Fincra Pay-In Export Complete*\n"
                f"Period: `{START_DATE}` -> `{END_DATE}`\n"
                f"File: `{dest.name}` ({dest.stat().st_size // 1024} KB)"
            )
            print(f"\n[+] Done! File: {dest.resolve()}")

        except Exception as exc:
            msg = f"Fincra Pay-In FAILED\nPeriod: {START_DATE} -> {END_DATE}\nError: {exc}"
            print(f"\n[!] {msg}")
            notify_slack(msg, color="danger")
            raise
        finally:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())

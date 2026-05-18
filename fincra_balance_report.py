#!/usr/bin/env python3
"""
Fincra Pay-In Export

Date logic:
  - From Date : --start_date arg (required, from Google Sheets via n8n)
  - To Date   : --end_date arg (optional, defaults to today)

Usage:
    python fincra_balance_report.py --start_date 2026-04-01
    python fincra_balance_report.py --start_date 2026-04-01 --end_date 2026-05-04
"""

import argparse
import asyncio
import boto3
import csv
import os
import pyotp
import requests
from botocore.exceptions import BotoCoreError, ClientError
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
# Date range
# ---------------------------------------------------------------------------
def parse_date(d: str) -> str:
    for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(d, fmt).strftime("%Y-%m-%d")
        except Exception:
            pass
    return datetime.fromisoformat(d).strftime("%Y-%m-%d")

_parser = argparse.ArgumentParser()
_parser.add_argument("--start_date", type=str, required=True)
_parser.add_argument("--end_date",   type=str, required=False)
_args = _parser.parse_args()

START_DATE = parse_date(_args.start_date)
END_DATE   = parse_date(_args.end_date) if _args.end_date else datetime.now().strftime("%Y-%m-%d")

START_DT = datetime.strptime(START_DATE, "%Y-%m-%d")
END_DT   = datetime.strptime(END_DATE,   "%Y-%m-%d")

print(f"[*] Date range: {START_DATE} -> {END_DATE}")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
USERNAME        = os.environ.get("FINCRA_USERNAME", "")
PASSWORD        = os.environ.get("FINCRA_PASSWORD", "")
TOTP_SECRET     = os.environ.get("FINCRA_TOTP_SECRET", "")
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
SLACK_USER_ID   = os.environ.get("SLACK_USER_ID", "")

AWS_ACCESS_KEY_ID     = os.environ.get("AWS_ACCESS_KEY_ID", "")
AWS_SECRET_ACCESS_KEY = os.environ.get("AWS_SECRET_ACCESS_KEY", "")
AWS_REGION            = os.environ.get("AWS_REGION", "ap-southeast-1")
S3_BUCKET             = os.environ.get("S3_BUCKET", "payout-recon")
S3_PREFIX             = os.environ.get("S3_PREFIX", "fincra/collect/raw/")


def to_file_date(d: datetime) -> str:
    return d.strftime("%d%m%Y")


PAYIN_FILENAME = f"FINCRA_PAYIN_{to_file_date(START_DT)}_to_{to_file_date(END_DT)}.csv"
DOWNLOAD_DIR   = Path("downloads")
LOGIN_URL      = "https://app.fincra.com/auth/login"
API_BASE       = "https://app.fincra.com/api/collections"


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
# S3 Upload
# ---------------------------------------------------------------------------
def upload_to_s3(local_path: Path) -> str:
    s3_key = f"{S3_PREFIX}{local_path.name}"
    print(f"[s3] Uploading {local_path.name} -> s3://{S3_BUCKET}/{s3_key} ...")
    try:
        client = boto3.client(
            "s3",
            region_name=AWS_REGION,
            aws_access_key_id=AWS_ACCESS_KEY_ID,
            aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
        )
        client.upload_file(str(local_path), S3_BUCKET, s3_key)
        s3_uri = f"s3://{S3_BUCKET}/{s3_key}"
        print(f"[s3] Upload complete: {s3_uri}")
        return s3_uri
    except (BotoCoreError, ClientError) as e:
        print(f"[s3] Upload failed: {e}")
        raise


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------
async def do_login(page) -> None:
    print("[login] Navigating to login page ...")
    await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30_000)
    await page.wait_for_timeout(2_000)

    email_sel = 'input[type="email"], input[name="email"], input[placeholder*="email" i]'
    await page.wait_for_selector(email_sel, timeout=15_000)
    await page.fill(email_sel, USERNAME)
    await page.wait_for_timeout(400)
    await page.fill('input[type="password"]', PASSWORD)
    await page.wait_for_timeout(400)

    for btn_sel in ['button:has-text("Log in")', 'button:has-text("Login")',
                    'button:has-text("Sign in")', 'button[type="submit"]']:
        loc = page.locator(btn_sel)
        if await loc.count() > 0:
            await loc.first.click()
            print(f"[login] Clicked login button via: {btn_sel}")
            break

    await page.wait_for_timeout(4_000)

    for attempt in range(1, 4):
        if "twofa" not in page.url and "verify" not in page.url:
            break

        print(f"[login] 2FA detected. Attempt {attempt} ...")
        code = get_otp()
        print(f"[login] OTP: {code}")

        inputs = page.locator('input[name^="otp-code"]')
        if await inputs.count() >= 6:
            for i, digit in enumerate(code):
                await inputs.nth(i).click()
                await page.wait_for_timeout(80)
                await inputs.nth(i).fill(digit)
                await page.wait_for_timeout(80)
        else:
            single = page.locator('input[maxlength="6"], input[type="number"]')
            if await single.count() > 0:
                await single.first.fill(code)

        for s in ['button:has-text("Verify")', 'button:has-text("Submit")',
                  'button:has-text("Confirm")', 'button[type="submit"]']:
            loc = page.locator(s)
            if await loc.count() > 0:
                await loc.first.click()
                break
        else:
            await page.keyboard.press("Enter")

        await page.wait_for_timeout(4_000)

        if "dashboard" in page.url or "payins" in page.url or "overview" in page.url:
            break
        if attempt < 3:
            print("[login] OTP not accepted — waiting for next TOTP window ...")
            await page.wait_for_timeout(15_000)

    await _dismiss_survey(page)
    final_url = page.url
    print(f"[login] Done. URL: {final_url}")
    if "login" in final_url or "auth" in final_url:
        await ss(page, "fail_login")
        raise RuntimeError(f"Login failed — still on auth page: {final_url}")


async def _dismiss_survey(page) -> None:
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=8_000)
    except Exception:
        pass
    await page.wait_for_timeout(1_000)
    try:
        await page.evaluate(
            "() => document.querySelectorAll('.ReactModal__Overlay').forEach(e => e.remove())"
        )
    except Exception:
        pass
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
    try:
        if await page.locator('.ReactModal__Overlay').count() > 0:
            await page.keyboard.press('Escape')
            await page.wait_for_timeout(600)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Capture auth token + business ID (works headed and headless)
# ---------------------------------------------------------------------------
async def get_auth_and_business(page) -> tuple[str, str]:
    """
    Navigate to payins page and capture:
      - Authorization Bearer token  (from any /api/ request header)
      - business ID                 (from the ?business= query param)

    We capture ONLY these two values and build the API URL ourselves —
    so wrong or missing date params on the auto-load call cannot affect results.
    """
    print("[auth] Navigating to payins page to capture auth token + business ID ...")
    captured: dict = {"auth": "", "business_id": ""}

    async def on_request(request):
        if captured["auth"] and captured["business_id"]:
            return
        if "/api/" not in request.url:
            return
        try:
            hdrs = dict(await request.all_headers())
            auth = hdrs.get("authorization", "")
            if auth.startswith("Bearer ") and not captured["auth"]:
                captured["auth"] = auth
                print(f"[auth] Token captured from: {request.url}")
        except Exception:
            pass
        if not captured["business_id"] and "business=" in request.url:
            qs = parse_qs(urlparse(request.url).query)
            bid = qs.get("business", [""])[0]
            if bid:
                captured["business_id"] = bid
                print(f"[auth] Business ID captured: {bid}")

    page.on("request", on_request)
    await page.goto("https://app.fincra.com/payins", wait_until="domcontentloaded", timeout=30_000)
    await page.wait_for_timeout(5_000)
    await _dismiss_survey(page)
    page.remove_listener("request", on_request)

    if not captured["auth"]:
        raise RuntimeError("[auth] Could not capture auth token — no /api/ calls observed")
    if not captured["business_id"]:
        raise RuntimeError("[auth] Could not capture business ID from API calls")

    return captured["auth"], captured["business_id"]


# ---------------------------------------------------------------------------
# Fetch all pay-in records directly via API with correct dates
# ---------------------------------------------------------------------------
async def fetch_all_payins(context, auth_token: str, business_id: str) -> list[dict]:
    """
    Calls the collections API directly using the known URL structure.
    Dates come from START_DATE / END_DATE — never from a captured URL.
    """
    all_rows: list[dict] = []
    page_num = 1
    per_page = 100

    while True:
        params = {
            "business":           business_id,
            "page":               str(page_num),
            "perPage":            str(per_page),
            "includeSubAccounts": "false",
            "dateInitiatedFrom":  START_DATE,
            "dateInitiatedTo":    END_DATE,
        }
        fetch_url = f"{API_BASE}?{urlencode(params)}"
        print(f"[payin-api] Page {page_num}: {fetch_url}")

        resp = await context.request.get(fetch_url, headers={"authorization": auth_token})
        if not resp.ok:
            print(f"[payin-api] Error {resp.status}: {await resp.text()}")
            break

        try:
            body = await resp.json()
        except Exception:
            print(f"[payin-api] Non-JSON response: {(await resp.text())[:200]}")
            break

        if not isinstance(body, dict):
            print(f"[payin-api] Unexpected body type: {type(body)}")
            break

        inner = body.get("data", body)
        if isinstance(inner, dict):
            records = (inner.get("results") or inner.get("data") or
                       inner.get("records") or inner.get("transactions") or [])
        elif isinstance(inner, list):
            records = inner
        else:
            records = []

        if not records:
            print("[payin-api] No more records.")
            break

        all_rows.extend(records)
        print(f"[payin-api] Page {page_num}: {len(records)} rows | total so far: {len(all_rows)}")

        total = (inner.get("total") or 0) if isinstance(inner, dict) else 0
        if (total and len(all_rows) >= int(total)) or len(records) < per_page:
            break
        page_num += 1

    return all_rows


# ---------------------------------------------------------------------------
# Export Pay-Ins
# ---------------------------------------------------------------------------
async def export_payins(page, context) -> Path:
    print(f"\n[payin] Exporting {START_DATE} -> {END_DATE} ...")
    await ss(page, "01_start")

    auth_token, business_id = await get_auth_and_business(page)
    await ss(page, "02_auth_captured")

    all_rows = await fetch_all_payins(context, auth_token, business_id)

    if not all_rows:
        raise RuntimeError("[payin] No records fetched — check date range or credentials")

    DOWNLOAD_DIR.mkdir(exist_ok=True)
    dest = DOWNLOAD_DIR / PAYIN_FILENAME

    all_keys: list[str] = []
    seen: set[str] = set()
    for row in all_rows:
        for k in row:
            if k not in seen:
                all_keys.append(k)
                seen.add(k)

    with open(dest, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=all_keys, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(all_rows)

    print(f"[payin] Saved {len(all_rows)} records -> {dest.resolve()}")
    await ss(page, "03_done")
    return dest


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
            dest   = await export_payins(page, context)
            s3_uri = upload_to_s3(dest)
            notify_slack(
                f":white_check_mark: *Fincra Pay-In Export Complete*\n"
                f"Period: `{START_DATE}` -> `{END_DATE}`\n"
                f"File: `{dest.name}` ({dest.stat().st_size // 1024} KB)\n"
                f"S3: `{s3_uri}`"
            )
            print(f"\n[+] Done! File: {dest.resolve()}")
            print(f"[+] S3:   {s3_uri}")
        except Exception as exc:
            msg = f"Fincra Pay-In FAILED\nPeriod: {START_DATE} -> {END_DATE}\nError: {exc}"
            print(f"\n[!] {msg}")
            notify_slack(msg, color="danger")
            raise
        finally:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())

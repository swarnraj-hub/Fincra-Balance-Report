#!/usr/bin/env python3
"""
Fincra Pay-In Export

Date logic:
  - From Date : --start_date arg (required, from Google Sheets via n8n)
  - To Date   : --end_date arg (optional, defaults to today)

Usage:
    python fincra_payin.py --start_date 2026-04-01
    python fincra_payin.py --start_date 2026-04-01 --end_date 2026-05-04
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
# Calendar helpers
# ---------------------------------------------------------------------------
async def _calendar_nav_to(page, target_month: int, target_year: int) -> None:
    set_ok = await page.evaluate(f"""() => {{
        const cal = document.querySelector(
            '.rdrCalendarWrapper, .rdrDateRangeWrapper, .rdrDateRangePickerWrapper');
        if (!cal) return false;
        const selects = cal.querySelectorAll('select');
        let mSet = false, ySet = false;
        for (const s of selects) {{
            const html = s.innerHTML;
            if (html.includes('value="0"') && html.includes('value="11"')) {{
                s.value = String({target_month - 1});
                s.dispatchEvent(new Event('change', {{bubbles: true}}));
                mSet = true;
            }} else if (s.options.length >= 3) {{
                const y = parseInt(s.value);
                if (y > 2000 && y < 2100) {{
                    s.value = String({target_year});
                    s.dispatchEvent(new Event('change', {{bubbles: true}}));
                    ySet = true;
                }}
            }}
        }}
        return mSet && ySet;
    }}""")
    if set_ok:
        await page.wait_for_timeout(300)
        print(f"[cal] Set calendar to {target_month}/{target_year} via JS selects")
        return

    now = datetime.now()
    months_diff = (now.year - target_year) * 12 + (now.month - target_month)
    print(f"[cal] Navigating {months_diff} months via arrows")
    for _ in range(abs(months_diff)):
        if months_diff > 0:
            btn = page.locator('.rdrPprevButton, .rdrPrevButton')
            if await btn.count() == 0:
                btn = page.locator('button[aria-label*="previous" i]')
        else:
            btn = page.locator('.rdrNextButton')
            if await btn.count() == 0:
                btn = page.locator('button[aria-label*="next" i]')
        if await btn.count() > 0:
            await btn.first.click()
            await page.wait_for_timeout(300)
        else:
            break


async def _click_calendar_day(page, day: int) -> bool:
    day_str = str(day)
    for cal_sel in ['.rdrCalendarWrapper', '.rdrDateRangeWrapper', '.rdrDateRangePickerWrapper']:
        cal = page.locator(cal_sel)
        if await cal.count() > 0:
            for tag in ['button', 'td', 'span', 'div']:
                candidates = cal.locator(tag).filter(has_text=day_str)
                for i in range(await candidates.count()):
                    el = candidates.nth(i)
                    if (await el.text_content() or "").strip() != day_str:
                        continue
                    try:
                        await el.click(timeout=3_000)
                        print(f"[cal] Clicked day {day_str}")
                        return True
                    except Exception:
                        continue
    result = await page.evaluate(f"""() => {{
        const cal = document.querySelector(
            '.rdrCalendarWrapper, .rdrDateRangeWrapper, .rdrDateRangePickerWrapper') || document;
        for (const el of cal.querySelectorAll('button, td, span, div')) {{
            if ((el.textContent || '').trim() === '{day_str}' && el.offsetParent !== null) {{
                el.click(); return true;
            }}
        }}
        return false;
    }}""")
    return bool(result)


async def _is_calendar_open(page) -> bool:
    return await page.locator(
        '.rdrCalendarWrapper, .rdrDateRangeWrapper, .rdrDateRangePickerWrapper'
    ).count() > 0


async def _click_date_dropdown(page, index: int) -> None:
    await page.evaluate(f"""() => {{
        const els = Array.from(document.querySelectorAll('*'))
            .filter(el => (el.textContent || '').trim() === 'Select Date'
                       && el.offsetParent !== null);
        const el = els[{index}];
        if (el) {{
            const target = el.closest('button, [role="button"], [class*="select"], [class*="dropdown"], [class*="picker"]')
                        || el.parentElement || el;
            target.click();
        }}
    }}""")


async def _set_date_range(page, label: str = "") -> None:
    pfx = f"[{label}]"
    count = await page.evaluate("""() =>
        Array.from(document.querySelectorAll('*'))
            .filter(el => (el.textContent || '').trim() === 'Select Date'
                       && el.offsetParent !== null).length""")
    print(f"{pfx} Visible 'Select Date' dropdowns: {count}")

    print(f"{pfx} Opening From date picker ...")
    await _click_date_dropdown(page, 0)
    await page.wait_for_timeout(1_200)
    if not await _is_calendar_open(page):
        await page.get_by_text("Select Date", exact=True).first.click(timeout=5_000)
        await page.wait_for_timeout(1_200)

    await _calendar_nav_to(page, START_DT.month, START_DT.year)
    await _click_calendar_day(page, START_DT.day)
    await page.wait_for_timeout(600)
    if await _is_calendar_open(page):
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(400)

    print(f"{pfx} Opening To date picker ...")
    await _click_date_dropdown(page, 0)
    await page.wait_for_timeout(1_200)
    if not await _is_calendar_open(page):
        await page.get_by_text("Select Date", exact=True).first.click(timeout=5_000)
        await page.wait_for_timeout(1_200)

    await _calendar_nav_to(page, END_DT.month, END_DT.year)
    await _click_calendar_day(page, END_DT.day)
    await page.wait_for_timeout(600)
    if await _is_calendar_open(page):
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(400)


# ---------------------------------------------------------------------------
# Export Pay-Ins
# ---------------------------------------------------------------------------
async def export_payins(page, context) -> Path:
    print(f"\n[payin] Exporting {START_DATE} -> {END_DATE} ...")

    await page.goto("https://app.fincra.com/payins", wait_until="domcontentloaded", timeout=30_000)
    await page.wait_for_timeout(2_500)
    await _dismiss_survey(page)
    await ss(page, "01_payins_page")

    print("[payin] Clicking Show Filters ...")
    clicked = False
    try:
        await page.get_by_text("Show Filters").click(timeout=8_000)
        clicked = True
    except Exception:
        pass
    if not clicked:
        await page.evaluate("""() => {
            const el = [...document.querySelectorAll('*')]
                .find(e => (e.textContent || '').trim() === 'Show Filters'
                        && e.offsetParent !== null);
            if (el) (el.closest('button,[role="button"]') || el.parentElement || el).click();
        }""")
    await page.wait_for_timeout(1_500)
    await ss(page, "02_filters_open")

    print("[payin] Setting date range ...")
    await _set_date_range(page, label="payin")

    await page.keyboard.press("Escape")
    await page.wait_for_timeout(300)
    try:
        await page.locator('h1').first.click(timeout=2_000)
    except Exception:
        pass
    await page.wait_for_timeout(500)

    api_calls: list[dict] = []

    async def capture_request(request):
        if '/api/' in request.url and request.method in ('GET', 'POST'):
            try:
                hdrs = dict(await request.all_headers())
            except Exception:
                hdrs = {}
            api_calls.append({'url': request.url, 'headers': hdrs})

    page.on("request", capture_request)

    print("[payin] Clicking Search ...")
    searched = False
    for sel in ['button:has-text("Search")', 'text=Search']:
        loc = page.locator(sel)
        if await loc.count() > 0:
            try:
                await loc.first.click(timeout=8_000)
                searched = True
                break
            except Exception:
                continue
    if not searched:
        await page.evaluate("""() => {
            for (const btn of document.querySelectorAll('button')) {
                if ((btn.textContent || '').trim().startsWith('Search')) {
                    btn.click(); return;
                }
            }
        }""")

    await page.wait_for_timeout(5_000)
    await ss(page, "03_after_search")
    page.remove_listener("request", capture_request)

    data_call = next(
        (c for c in api_calls if any(k in c['url'].lower()
         for k in ('payin', 'collection', 'pay-in', 'pay_in'))),
        api_calls[0] if api_calls else None,
    )
    if not data_call:
        raise RuntimeError("[payin] Could not find data API endpoint")

    print(f"[payin] API endpoint: {data_call['url']}")

    parsed   = urlparse(data_call['url'])
    params   = parse_qs(parsed.query, keep_blank_values=True)
    base_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    auth_hdr = data_call['headers'].get('authorization', '')

    page_size_key = next(
        (k for k in params if k.lower() in ('perpage', 'per_page', 'limit', 'pagesize')), 'perPage')
    page_key = next((k for k in params if k.lower() == 'page'), 'page')

    for k in list(params):
        kl = k.lower()
        if kl in ('dateinitiatedfrom', 'startdate', 'start_date', 'from', 'datefrom'):
            params[k] = [START_DATE]
        elif kl in ('dateinitiatedto', 'enddate', 'end_date', 'to', 'dateto'):
            params[k] = [END_DATE]

    all_rows: list[dict] = []
    page_num, per_page = 1, 100

    while True:
        params[page_key]      = [str(page_num)]
        params[page_size_key] = [str(per_page)]
        fetch_url = f"{base_url}?{urlencode({k: v[0] for k, v in params.items()})}"
        print(f"[payin-api] Page {page_num}: {fetch_url}")

        resp = await context.request.get(fetch_url, headers={'authorization': auth_hdr})
        if not resp.ok:
            print(f"[payin-api] Error {resp.status}")
            break

        try:
            body = await resp.json()
        except Exception:
            print(f"[payin-api] Non-JSON response: {(await resp.text())[:200]}")
            break

        if not isinstance(body, dict):
            print(f"[payin-api] Unexpected body type: {type(body)}")
            break

        inner = body.get('data', body)
        if isinstance(inner, dict):
            records = (inner.get('results') or inner.get('data') or
                       inner.get('records') or inner.get('transactions') or [])
        elif isinstance(inner, list):
            records = inner
        else:
            records = []

        if not records:
            print("[payin-api] No more records.")
            break

        all_rows.extend(records)
        print(f"[payin-api] Page {page_num}: {len(records)} rows | total: {len(all_rows)}")

        total = (inner.get('total') or 0) if isinstance(inner, dict) else 0
        if (total and len(all_rows) >= int(total)) or len(records) < per_page:
            break
        page_num += 1

    if not all_rows:
        raise RuntimeError("[payin] No records fetched")

    DOWNLOAD_DIR.mkdir(exist_ok=True)
    dest = DOWNLOAD_DIR / PAYIN_FILENAME
    all_keys: list[str] = []
    seen: set[str] = set()
    for row in all_rows:
        for k in row:
            if k not in seen:
                all_keys.append(k)
                seen.add(k)

    with open(dest, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=all_keys, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(all_rows)

    print(f"[payin] Saved {len(all_rows)} records -> {dest.resolve()}")
    await ss(page, "04_done")
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

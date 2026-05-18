#!/usr/bin/env python3
"""
Fincra Pay-In Export

Logs into Fincra, sets the date filter, clicks Search then Export.
Fincra emails a CSV download link to the logged-in user — no file handling needed here.

Usage:
    python fincra_balance_report.py --start_date 2025-06-11
    python fincra_balance_report.py --start_date 2025-06-11 --end_date 2025-06-19

Date formats accepted: YYYY-MM-DD or DD/MM/YYYY
If --end_date is omitted it defaults to today.
"""

import argparse
import asyncio
import os
import re
import pyotp
import requests
from datetime import datetime
from pathlib import Path
from playwright.async_api import async_playwright

# ---------------------------------------------------------------------------
# Load .env  (key=value, ignores # comments)
# ---------------------------------------------------------------------------
_ENV_FILE = Path(__file__).parent / ".env"
if _ENV_FILE.exists():
    for _line in _ENV_FILE.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

# ---------------------------------------------------------------------------
# CLI arguments  — fully dynamic dates
# ---------------------------------------------------------------------------
def _parse_date(d: str) -> str:
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(d, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    raise ValueError(f"Unrecognised date format: {d!r}  (expected YYYY-MM-DD or DD/MM/YYYY)")

_ap = argparse.ArgumentParser(description="Fincra Pay-In Export")
_ap.add_argument("--start_date", required=True,  help="From date  YYYY-MM-DD or DD/MM/YYYY")
_ap.add_argument("--end_date",   required=False, help="To date    YYYY-MM-DD or DD/MM/YYYY (default: today)")
_args = _ap.parse_args()

START_DATE = _parse_date(_args.start_date)
END_DATE   = _parse_date(_args.end_date) if _args.end_date else datetime.now().strftime("%Y-%m-%d")
START_DT   = datetime.strptime(START_DATE, "%Y-%m-%d")
END_DT     = datetime.strptime(END_DATE,   "%Y-%m-%d")

if START_DT > END_DT:
    raise SystemExit(f"[error] start_date {START_DATE} is after end_date {END_DATE}")

# ---------------------------------------------------------------------------
# Config from environment
# ---------------------------------------------------------------------------
USERNAME        = os.environ.get("FINCRA_USERNAME", "")
PASSWORD        = os.environ.get("FINCRA_PASSWORD", "")
TOTP_SECRET     = os.environ.get("FINCRA_TOTP_SECRET", "")
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
SLACK_USER_ID   = os.environ.get("SLACK_USER_ID", "")

LOGIN_URL = "https://app.fincra.com/auth/login"
MONTH_NAMES = [
    "January","February","March","April","May","June",
    "July","August","September","October","November","December",
]
MONTH_ABBREVS = [
    "Jan","Feb","Mar","Apr","May","Jun",
    "Jul","Aug","Sep","Oct","Nov","Dec",
]


def _get_otp() -> str:
    return pyotp.TOTP(TOTP_SECRET).now()


async def _ss(page, label: str) -> None:
    path = f"fincra_{label}.png"
    await page.screenshot(path=path, full_page=False)
    print(f"  [ss] {path}")


# ---------------------------------------------------------------------------
# Slack DM helper
# ---------------------------------------------------------------------------
def notify_slack(message: str, color: str = "good") -> None:
    if not SLACK_BOT_TOKEN or not SLACK_USER_ID:
        return
    hdrs = {"Authorization": f"Bearer {SLACK_BOT_TOKEN}", "Content-Type": "application/json"}
    channel = SLACK_USER_ID
    if SLACK_USER_ID.startswith("U"):
        try:
            r = requests.post(
                "https://slack.com/api/conversations.open",
                json={"users": SLACK_USER_ID}, headers=hdrs, timeout=10,
            )
            if r.ok and r.json().get("ok"):
                channel = r.json()["channel"]["id"]
        except Exception:
            pass
    icon = {"good": ":white_check_mark:", "danger": ":x:", "warning": ":warning:"}.get(color, "")
    try:
        requests.post(
            "https://slack.com/api/chat.postMessage",
            json={"channel": channel, "text": f"{icon} {message}"},
            headers=hdrs, timeout=10,
        )
    except Exception as e:
        print(f"[slack] error: {e}")


# ---------------------------------------------------------------------------
# LOGIN
# ---------------------------------------------------------------------------
async def do_login(page) -> None:
    print(f"[login] -> {LOGIN_URL}")
    await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30_000)
    await page.wait_for_timeout(2_000)

    await page.wait_for_selector('input[type="email"]', timeout=15_000)
    await page.fill('input[type="email"]', USERNAME)
    await page.wait_for_timeout(300)
    await page.fill('input[type="password"]', PASSWORD)
    await page.wait_for_timeout(300)

    for sel in ['button:has-text("Log in")', 'button:has-text("Login")',
                'button:has-text("Sign in")', 'button[type="submit"]']:
        if await page.locator(sel).count() > 0:
            await page.locator(sel).first.click()
            print(f"[login] clicked {sel}")
            break

    await page.wait_for_timeout(4_000)

    # 2FA loop — up to 3 attempts (each TOTP window is 30 s)
    for attempt in range(1, 4):
        if "twofa" not in page.url and "verify" not in page.url:
            break
        code = _get_otp()
        print(f"[login] 2FA attempt {attempt} — OTP {code}")
        inputs = page.locator('input[name^="otp-code"]')
        if await inputs.count() >= 6:
            for i, digit in enumerate(code):
                await inputs.nth(i).click()
                await inputs.nth(i).fill(digit)
                await page.wait_for_timeout(60)
        else:
            sel = page.locator('input[maxlength="6"], input[type="number"]')
            if await sel.count() > 0:
                await sel.first.fill(code)
        for s in ['button:has-text("Verify")', 'button:has-text("Confirm")',
                  'button:has-text("Submit")', 'button[type="submit"]']:
            if await page.locator(s).count() > 0:
                await page.locator(s).first.click()
                break
        else:
            await page.keyboard.press("Enter")
        await page.wait_for_timeout(4_000)
        if any(k in page.url for k in ("dashboard", "payins", "overview")):
            break
        if attempt < 3:
            print("[login] OTP rejected — waiting 15 s for next window ...")
            await page.wait_for_timeout(15_000)

    await _dismiss_popups(page)
    print(f"[login] done — {page.url}")
    if any(k in page.url for k in ("login", "auth")):
        await _ss(page, "fail_login")
        raise RuntimeError(f"Login failed, still on: {page.url}")


# ---------------------------------------------------------------------------
# Dismiss survey / modal popups
# ---------------------------------------------------------------------------
async def _dismiss_popups(page) -> None:
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=8_000)
    except Exception:
        pass
    await page.wait_for_timeout(800)
    try:
        await page.evaluate(
            "() => document.querySelectorAll('.ReactModal__Overlay').forEach(e => e.remove())"
        )
    except Exception:
        pass
    for sel in [
        'button:has-text("Remind Me Later")', 'button:has-text("Remind me later")',
        'button:has-text("No thanks")',        'button:has-text("Dismiss")',
        'button:has-text("Skip")',             '[aria-label="Close"]',
    ]:
        try:
            loc = page.locator(sel)
            if await loc.count() > 0:
                await loc.first.click(timeout=3_000)
                await page.wait_for_timeout(400)
                break
        except Exception:
            pass
    try:
        if await page.locator(".ReactModal__Overlay").count() > 0:
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(400)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# CALENDAR — read the currently displayed month & year
# ---------------------------------------------------------------------------
async def _get_cal_month_year(page) -> tuple[int, int]:
    result = await page.evaluate(f"""() => {{
        const MONTHS  = {MONTH_NAMES!r};
        const ABBREVS = {MONTH_ABBREVS!r};
        let month = 0, year = 0;

        for (const s of document.querySelectorAll('select')) {{
            if (!s.offsetParent) continue;
            const sel = s.options[s.selectedIndex];
            if (!sel) continue;
            const text = sel.text.trim();
            const mi = MONTHS.indexOf(text);
            if (mi >= 0) {{ month = mi + 1; continue; }}
            const ai = ABBREVS.indexOf(text);
            if (ai >= 0) {{ month = ai + 1; continue; }}
            const valNum  = parseInt(sel.value);
            if (!isNaN(valNum) && valNum > 2000 && valNum < 2100) {{ year = valNum; continue; }}
            const textNum = parseInt(text);
            if (!isNaN(textNum) && textNum > 2000 && textNum < 2100) {{ year = textNum; continue; }}
        }}

        if (month === 0 || year === 0) {{
            for (const el of document.querySelectorAll('*')) {{
                if (!el.offsetParent) continue;
                const vis = [...el.children].filter(c => c.offsetParent !== null);
                if (vis.length > 1) continue;
                const text = (el.textContent || '').trim();
                if (month === 0) {{
                    const mi = MONTHS.indexOf(text);
                    if (mi >= 0) {{ month = mi + 1; continue; }}
                    const ai = ABBREVS.indexOf(text);
                    if (ai >= 0) {{ month = ai + 1; continue; }}
                }}
                if (year === 0) {{
                    const y = parseInt(text);
                    if (!isNaN(y) && y > 2000 && y < 2100) {{ year = y; }}
                }}
            }}
        }}
        return {{ month, year }};
    }}""")
    return result["month"], result["year"]


# ---------------------------------------------------------------------------
# CALENDAR — navigate to the correct month/year
# ---------------------------------------------------------------------------
async def _nav_to_month(page, month: int, year: int) -> None:
    target_name = MONTH_NAMES[month - 1]
    month_val   = str(month - 1)   # 0-indexed: Jan=0 … Dec=11
    year_val    = str(year)

    cur_m, cur_y = await _get_cal_month_year(page)
    if cur_m == month and cur_y == year:
        print(f"[cal] already at {target_name} {year}")
        return

    set_result = await page.evaluate(f"""() => {{
        function setReactSelectValue(el, value) {{
            if (!el) return false;
            const nativeSetter = Object.getOwnPropertyDescriptor(
                window.HTMLSelectElement.prototype, 'value'
            ).set;
            const prev = el.value;
            nativeSetter.call(el, value);
            if (el._valueTracker) {{ el._valueTracker.setValue(prev); }}
            el.dispatchEvent(new Event('change', {{ bubbles: true }}));
            return el.value === value;
        }}
        const mSel = document.querySelector('.rdrMonthPicker select');
        const ySel = document.querySelector('.rdrYearPicker select');
        const mOk = setReactSelectValue(mSel, '{month_val}');
        const yOk = setReactSelectValue(ySel, '{year_val}');
        return {{ mOk, yOk }};
    }}""")

    await page.wait_for_timeout(500)

    cur_m, cur_y = await _get_cal_month_year(page)
    if cur_m == month and cur_y == year:
        print(f"[cal] at {target_name} {year} via React setter OK")
        return

    if cur_m != month:
        try:
            await page.locator('.rdrMonthPicker select').select_option(value=month_val, timeout=2_000)
            await page.wait_for_timeout(400)
        except Exception:
            pass
    if cur_y != year:
        try:
            await page.locator('.rdrYearPicker select').select_option(value=year_val, timeout=2_000)
            await page.wait_for_timeout(400)
        except Exception:
            pass

    cur_m, cur_y = await _get_cal_month_year(page)
    if cur_m == month and cur_y == year:
        print(f"[cal] at {target_name} {year} via select_option OK")
        return

    if cur_m == 0 or cur_y == 0:
        print(f"[cal] WARNING: calendar closed or unreadable after select attempts")
        return

    print(
        f"[cal] select attempts done but still at "
        f"{MONTH_NAMES[cur_m-1] if cur_m else '?'} {cur_y or '?'} "
        f"(want {target_name} {year}) — proceeding anyway"
    )


# ---------------------------------------------------------------------------
# CALENDAR — click a specific day reliably
# ---------------------------------------------------------------------------
async def _click_day(page, dt: datetime) -> None:
    day_str = str(dt.day)

    for cal_sel in ['.rdrCalendarWrapper', '.rdrDateRangeWrapper', '.rdrDateRangePickerWrapper']:
        cal_loc = page.locator(cal_sel)
        if await cal_loc.count() > 0:
            for tag in ['button', 'td', 'span', 'div']:
                candidates = cal_loc.locator(tag).filter(has_text=day_str)
                for i in range(await candidates.count()):
                    el = candidates.nth(i)
                    try:
                        if (await el.text_content() or "").strip() != day_str:
                            continue
                        await el.click(timeout=3_000)
                        print(f"[cal] clicked {dt.strftime('%Y-%m-%d')} via {cal_sel}>{tag}")
                        return
                    except Exception:
                        continue

    result = await page.evaluate(f"""() => {{
        const day = '{day_str}';
        const calRoot = document.querySelector(
            '.rdrCalendarWrapper,.rdrDateRangeWrapper,.rdrDateRangePickerWrapper') || document;

        for (const span of calRoot.querySelectorAll('.rdrDayNumber span')) {{
            if (span.textContent.trim() !== day) continue;
            const btn = span.closest('.rdrDay');
            if (!btn || btn.classList.contains('rdrDayPassive')) continue;
            if (btn.classList.contains('rdrDayDisabled')) continue;
            if (!btn.offsetParent) continue;
            btn.click(); return 'ok-rdr';
        }}

        for (const root of [calRoot, document]) {{
            const candidates = [];
            for (const el of root.querySelectorAll('td,button,span,div')) {{
                if (!el.offsetParent) continue;
                if ((el.textContent || '').trim() !== day) continue;
                const visKids = [...el.children].filter(c => c.offsetParent !== null);
                if (visKids.length > 1) continue;
                candidates.push(el);
            }}
            const active = candidates.filter(el => {{
                if (el.disabled || el.hasAttribute('disabled')) return false;
                const cls = [
                    el.className || '',
                    el.parentElement?.className || '',
                    el.closest('td,button')?.className || '',
                ].join(' ').toLowerCase();
                if (/disabled|passive|other|outside|inactive|grey|gray|dim/.test(cls)) return false;
                const op = parseFloat(window.getComputedStyle(el).opacity);
                if (!isNaN(op) && op < 0.5) return false;
                return true;
            }});
            if (active.length > 0) {{ active[0].click(); return 'ok-generic(' + root.className + ')'; }}
        }}
        return 'not-found';
    }}""")

    if result.startswith("ok"):
        print(f"[cal] clicked {dt.strftime('%Y-%m-%d')} ({result})")
    else:
        print(f"[cal] WARNING: could not click {dt.strftime('%Y-%m-%d')}")


async def _calendar_is_open(page) -> bool:
    if await page.locator(
        ".rdrCalendarWrapper, .rdrDateRangeWrapper, .rdrDateRangePickerWrapper"
    ).count() > 0:
        return True
    result = await page.evaluate(f"""() => {{
        const MONTHS  = {MONTH_NAMES!r};
        const ABBREVS = {MONTH_ABBREVS!r};
        for (const s of document.querySelectorAll('select')) {{
            if (!s.offsetParent) continue;
            const opts = [...s.options].map(o => o.text.trim());
            if (opts.some(t => MONTHS.includes(t) || ABBREVS.includes(t))) return true;
        }}
        return false;
    }}""")
    return result


# ---------------------------------------------------------------------------
# FILTER — open a "Select Date" picker and choose a date
# ---------------------------------------------------------------------------
async def _pick_date(page, label: str, dt: datetime) -> None:
    print(f"[filter] opening {label} picker -> {dt.strftime('%Y-%m-%d')}")

    # Always click the trigger for "To" (must open calendar in "To mode").
    # For "From" only click if the calendar is not already open.
    needs_trigger = (label == "To") or (not await _calendar_is_open(page))

    if needs_trigger:
        await page.evaluate("""() => {
            const triggers = [...document.querySelectorAll('*')]
                .filter(el => (el.textContent || '').trim() === 'Select Date'
                           && el.offsetParent !== null);
            const el = triggers[0];
            if (!el) return;
            (el.closest('button,[role="button"],[class*="select"],[class*="picker"]')
                || el.parentElement || el).click();
        }""")
        await page.wait_for_timeout(1_500)

    if not await _calendar_is_open(page):
        try:
            await page.get_by_text("Select Date", exact=True).first.click(timeout=5_000)
            await page.wait_for_timeout(1_500)
        except Exception:
            pass

    if not await _calendar_is_open(page):
        print(f"[filter] WARNING: calendar did not open for {label} — check screenshot")
        return

    await _nav_to_month(page, dt.month, dt.year)
    await page.wait_for_timeout(500)
    await _click_day(page, dt)
    await page.wait_for_timeout(800)

    if label == "To" and await _calendar_is_open(page):
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(400)


# ---------------------------------------------------------------------------
# EXPORT FLOW: Show Filters → From → To → Search → Export
# ---------------------------------------------------------------------------
async def run_export(page) -> None:
    print(f"\n[export] {START_DATE} -> {END_DATE}")

    await page.goto("https://app.fincra.com/payins", wait_until="domcontentloaded", timeout=30_000)
    await page.wait_for_timeout(3_000)
    await _dismiss_popups(page)
    await _ss(page, "01_payins")

    # Show Filters
    print("[filter] clicking Show Filters")
    clicked = False
    try:
        await page.get_by_text("Show Filters").click(timeout=8_000)
        clicked = True
    except Exception:
        pass
    if not clicked:
        await page.evaluate("""() => {
            const el = [...document.querySelectorAll('*')]
                .find(e => (e.textContent||'').trim() === 'Show Filters'
                        && e.offsetParent !== null);
            if (el) (el.closest('button,[role="button"]') || el.parentElement || el).click();
        }""")
    await page.wait_for_timeout(2_000)
    await _ss(page, "02_filters_open")

    # From date
    await _pick_date(page, "From", START_DT)
    await _ss(page, "03_from_set")

    # Close the From calendar before opening To — Fincra's calendar stays in
    # "From mode" after the first click, so we must close it first.
    if await _calendar_is_open(page):
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(700)
    print("[filter] From calendar closed, waiting before To ...")
    await page.wait_for_timeout(500)

    # To date
    await _pick_date(page, "To", END_DT)
    await _ss(page, "04_to_set")

    # Dismiss any leftover overlay before Search
    await page.keyboard.press("Escape")
    await page.wait_for_timeout(400)
    try:
        await page.locator("h1").first.click(timeout=2_000)
    except Exception:
        pass
    await page.wait_for_timeout(500)

    # Search
    print("[filter] clicking Search")
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
            for (const btn of document.querySelectorAll('button'))
                if ((btn.textContent||'').trim().startsWith('Search')) { btn.click(); return; }
        }""")
    await page.wait_for_timeout(4_000)
    await _ss(page, "05_searched")

    # Export
    print("[export] clicking Export")
    export_clicked = False
    for sel in ['button:has-text("Export")', 'a:has-text("Export")']:
        loc = page.locator(sel)
        if await loc.count() > 0:
            try:
                await loc.first.click(timeout=8_000)
                export_clicked = True
                print(f"[export] clicked via '{sel}'")
                break
            except Exception:
                continue
    if not export_clicked:
        await page.evaluate("""() => {
            for (const el of document.querySelectorAll('button,a'))
                if ((el.textContent||'').trim().toLowerCase().startsWith('export'))
                    { el.click(); return; }
        }""")

    await page.wait_for_timeout(2_000)
    await _ss(page, "06_exported")
    print(f"[export] done — Fincra will email the download link to {USERNAME}")


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
async def main() -> None:
    print("=" * 55)
    print("[*] Fincra Pay-In Export")
    print(f"[*] From  : {START_DATE}")
    print(f"[*] To    : {END_DATE}")
    print(f"[*] Email : {USERNAME}")
    print("=" * 55)

    is_ci = os.environ.get("CI", "false").lower() == "true"

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=is_ci,
            slow_mo=0 if is_ci else 80,
        )
        ctx  = await browser.new_context(viewport={"width": 1440, "height": 900})
        page = await ctx.new_page()
        try:
            await do_login(page)
            await run_export(page)
            notify_slack(
                f"*Fincra Pay-In Export Triggered* \n"
                f"Period : `{START_DATE}` to `{END_DATE}`\n"
                f"Link will be emailed to `{USERNAME}`"
            )
            print(f"\n[+] All done — check {USERNAME} inbox for the download link.")
        except Exception as exc:
            msg = f"Fincra Pay-In FAILED | {START_DATE} -> {END_DATE} | {exc}"
            print(f"[!] {msg}")
            notify_slack(msg, color="danger")
            raise
        finally:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())

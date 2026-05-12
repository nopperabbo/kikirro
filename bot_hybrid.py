#!/usr/bin/env python3
"""
Kiro Hybrid Bot — Token Harvester (Playwright) + Stripe Link Generator (Pure API)

Flow:
  PHASE 1: Patchright stealth browser logs in via Google SSO → captures AccessToken +
           CsrfToken + UserId + VisitorId from network traffic / cookies → closes browser.
  PHASE 2: httpx fires POST /GenerateSubscriptionManagementUrl (CBOR/Smithy RPC) to mint
           a Stripe Checkout URL tied to that token.

Bulletproof: per-account try/except, Google anti-bot detection, auto-screenshots,
Playwright timeout trap, never crashes the whole run.

Inputs:  email.txt   (email:password per line)
Output:  hybrid_links.txt  (email:stripe_link OR email:ERROR_<reason>)
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import random
import sys
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from urllib.parse import unquote

import cbor2
import httpx
from patchright.async_api import (
    Browser,
    BrowserContext,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

# ──────────────────────────────────────────────────────────────────────────────
# CONFIG — all values overridable via env vars so users don't need to edit code.
# ──────────────────────────────────────────────────────────────────────────────


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    val = os.environ.get(name, "").strip().lower()
    if val in ("1", "true", "yes", "on"):
        return True
    if val in ("0", "false", "no", "off"):
        return False
    return default


BASE_DIR = Path(__file__).resolve().parent
INPUT_FILE = Path(os.environ.get("KIRO_EMAIL_FILE") or (BASE_DIR / "email.txt"))
OUTPUT_FILE = Path(os.environ.get("KIRO_OUTPUT_FILE") or (BASE_DIR / "hybrid_links.txt"))
REFRESH_TOKEN_FILE = Path(os.environ.get("KIRO_REFRESH_FILE") or (BASE_DIR / "refresh_tokens.txt"))
SCREENSHOT_DIR = Path(os.environ.get("KIRO_SCREENSHOT_DIR") or (BASE_DIR / "screenshots"))

KIRO_LOGIN_URL = "https://app.kiro.dev/"
KIRO_API_BASE = "https://app.kiro.dev/service/KiroWebPortalService/operation"
KIRO_SUB_ENDPOINT = f"{KIRO_API_BASE}/GenerateSubscriptionManagementUrl"

# Plan — env KIRO_PLAN=PRO|PRO_PLUS|POWER overrides.
_PLAN_ALIASES = {
    "PRO": "Q_DEVELOPER_STANDALONE_PRO",
    "PRO_PLUS": "Q_DEVELOPER_STANDALONE_PRO_PLUS",
    "POWER": "Q_DEVELOPER_STANDALONE_POWER",
}
_plan_env = os.environ.get("KIRO_PLAN", "PRO").upper().strip()
Q_SUBSCRIPTION_TYPE = _PLAN_ALIASES.get(_plan_env, _plan_env) or "Q_DEVELOPER_STANDALONE_PRO"

NAV_TIMEOUT_MS = _env_int("KIRO_NAV_TIMEOUT_MS", 60_000)
STEP_TIMEOUT_MS = _env_int("KIRO_STEP_TIMEOUT_MS", 30_000)
TOKEN_WAIT_SECONDS = _env_int("KIRO_TOKEN_WAIT_SECONDS", 90)
BUTTON_WAIT_SECONDS = _env_int("KIRO_BUTTON_WAIT_SECONDS", 30)
PHASE1_RETRIES = _env_int("KIRO_PHASE1_RETRIES", 1)

HEADLESS = _env_bool("KIRO_HEADLESS", True)
CONCURRENCY = _env_int("KIRO_CONCURRENCY", 1)

# Always request English UI so selectors stay predictable across regions.
# Users in non-EN locales should leave these as-is — Kiro & Google both honour them.
# These three are retained as soft global overrides: if profiles.json is
# missing/empty, pick_profile() returns _DEFAULT_PROFILE — at which point
# these env vars still don't do anything because _DEFAULT_PROFILE has its
# own locale/tz. They're kept for backwards compatibility only.
LOCALE = os.environ.get("KIRO_LOCALE", "en-US")
TIMEZONE_ID = os.environ.get("KIRO_TIMEZONE", "America/Los_Angeles")
ACCEPT_LANGUAGE = os.environ.get("KIRO_ACCEPT_LANG", "en-US,en;q=0.9")

# Google / Cognito red-flag phrases that mean "stop, this account is cooked".
GOOGLE_BLOCK_PHRASES = [
    "Verify it's you",
    "Wrong password",
    "Couldn't sign you in",
    "This browser or app may not be secure",
    "Confirm you're not a robot",
    "Couldn't find your Google Account",
    "Enter a valid email",
    "suspicious sign-in",
    "Sign-in blocked",
    "unusual activity",
    "temporarily disabled",
    "account has been disabled",
    "Akun Anda dinonaktifkan",
    "Tidak dapat menemukan Akun Google Anda",
]

CHROMIUM_ARGS_BASE = [
    "--disable-blink-features=AutomationControlled",
    "--disable-features=IsolateOrigins,site-per-process,AsyncDns",
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-infobars",
    "--disable-extensions",
    "--no-first-run",
    "--no-default-browser-check",
    "--password-store=basic",
    "--disable-ipv6",
    "--dns-prefetch-disable",
]

# ──────────────────────────────────────────────────────────────────────────────
# PROFILES — 100 diverse browser fingerprints to rotate per account.
#   KIRO_PROFILES_FILE      override path (default: ./profiles.json)
#   KIRO_PROFILE_MODE       hash | random | fixed:<id>   (default: hash)
#   KIRO_FORCE_PROFILE_ID   shortcut for mode=fixed:<id>
# If profiles.json is missing, we fall back to a single macOS/en-US default so
# the bot still works — but you lose the per-account rotation benefit.
# ──────────────────────────────────────────────────────────────────────────────

PROFILES_FILE = Path(os.environ.get("KIRO_PROFILES_FILE") or (BASE_DIR / "profiles.json"))
PROFILE_MODE = os.environ.get("KIRO_PROFILE_MODE", "hash").strip().lower()
FORCE_PROFILE_ID = os.environ.get("KIRO_FORCE_PROFILE_ID", "").strip()

_DEFAULT_PROFILE: dict[str, Any] = {
    "id": "default-mac-en-US",
    "platform": "macOS",
    "user_agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/147.0.0.0 Safari/537.36"
    ),
    "sec_ch_ua": '"Not=A?Brand";v="99", "Chromium";v="147", "Google Chrome";v="147"',
    "sec_ch_ua_platform": '"macOS"',
    "sec_ch_ua_mobile": "?0",
    "viewport": {"width": 1366, "height": 820},
    "locale": "en-US",
    "timezone_id": "America/Los_Angeles",
    "accept_language": "en-US,en;q=0.9",
}


def _load_profiles() -> list[dict[str, Any]]:
    if not PROFILES_FILE.exists():
        return []
    try:
        raw = json.loads(PROFILES_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"⚠️  failed to parse {PROFILES_FILE.name}: {e} — using default profile", flush=True)
        return []
    if isinstance(raw, dict) and "profiles" in raw:
        profs = raw["profiles"]
    elif isinstance(raw, list):
        profs = raw
    else:
        return []
    return [p for p in profs if isinstance(p, dict) and p.get("id")]


_PROFILES: list[dict[str, Any]] = _load_profiles()


def pick_profile(email: str) -> dict[str, Any]:
    """Pick a browser profile for the given email.

    Modes:
      hash     → stable mapping: same email always gets the same profile, so
                 retries don't suddenly "teleport" to a different country.
      random   → independent of email. Useful for stress-testing fingerprints.
      fixed:ID → always return the profile with that id. Debug / reproduction.

    Falls back to a built-in default profile if profiles.json is absent/empty.
    """
    if FORCE_PROFILE_ID:
        for p in _PROFILES:
            if p.get("id") == FORCE_PROFILE_ID:
                return p
        return _DEFAULT_PROFILE
    if PROFILE_MODE.startswith("fixed:"):
        wanted = PROFILE_MODE.split(":", 1)[1]
        for p in _PROFILES:
            if p.get("id") == wanted:
                return p
        return _DEFAULT_PROFILE
    if not _PROFILES:
        return _DEFAULT_PROFILE
    if PROFILE_MODE == "random":
        return random.choice(_PROFILES)
    # default: hash
    digest = hashlib.sha256(email.lower().encode("utf-8")).digest()
    idx = int.from_bytes(digest[:8], "big") % len(_PROFILES)
    return _PROFILES[idx]

# ──────────────────────────────────────────────────────────────────────────────
# DATA
# ──────────────────────────────────────────────────────────────────────────────


@dataclass
class KiroSession:
    """Everything needed to talk to Kiro's backend as a logged-in user."""

    access_token: str
    refresh_token: str
    csrf_token: str
    user_id: str
    visitor_id: str
    cookies: dict[str, str]
    profile: dict[str, Any]


@dataclass
class AccountResult:
    email: str
    status: str  # "OK" | "ERROR_<reason>"
    payload: str  # Stripe URL on success, error detail on failure


# ──────────────────────────────────────────────────────────────────────────────
# UTIL
# ──────────────────────────────────────────────────────────────────────────────


def log(email: str, msg: str) -> None:
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] [{email}] {msg}", flush=True)


def safe_slug(email: str) -> str:
    return email.replace("@", "_at_").replace("/", "_").replace(":", "_")


def read_accounts(path: Path) -> list[tuple[str, str]]:
    if not path.exists():
        raise FileNotFoundError(f"{path} not found")
    out: list[tuple[str, str]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        email, _, password = line.partition(":")
        email = email.strip()
        password = password.strip()
        if email and password:
            out.append((email, password))
    return out


def append_result(result: AccountResult) -> None:
    """Append atomically so the file is always usable even mid-run."""
    line = f"{result.email}:{result.payload}\n"
    with OUTPUT_FILE.open("a", encoding="utf-8") as f:
        f.write(line)


# ──────────────────────────────────────────────────────────────────────────────
# PHASE 1 — TOKEN HARVESTER
# ──────────────────────────────────────────────────────────────────────────────


class GoogleBlockedError(Exception):
    """Google threw up a captcha / verify / block screen."""


async def _page_contains_block_phrase(page: Page) -> Optional[str]:
    try:
        body_text = await page.evaluate(
            "() => document.body ? document.body.innerText : ''"
        )
    except Exception:
        return None
    low = body_text.lower()
    for phrase in GOOGLE_BLOCK_PHRASES:
        if phrase.lower() in low:
            return phrase
    return None


async def _raise_if_blocked(page: Page) -> None:
    phrase = await _page_contains_block_phrase(page)
    if phrase:
        raise GoogleBlockedError(phrase)


async def _decode_cbor_response(body: bytes) -> Optional[dict]:
    """Kiro responses are Smithy RPC-v2-CBOR (application/cbor)."""
    if not body:
        return None
    try:
        return cbor2.loads(body)
    except Exception:
        return None


async def harvest_tokens(email: str, password: str) -> KiroSession:
    """
    Drive Patchright through Google SSO, intercept the /ExchangeToken response,
    and return a fully-assembled KiroSession. Raises on any failure.
    """
    SCREENSHOT_DIR.mkdir(exist_ok=True)

    # We stash the captured payload here from the response listener.
    #   "exchange"      -> decoded CBOR body (accessToken, csrfToken, expiresIn, profileArn)
    #   "refresh_token" -> pulled from the Set-Cookie: RefreshToken=...; HttpOnly header
    #                      because Kiro does NOT put refreshToken in the response body.
    captured: dict[str, Any] = {"exchange": None, "refresh_token": None}

    profile = pick_profile(email)
    log(
        email,
        f"🎭 profile={profile.get('id')}  platform={profile.get('platform')}  "
        f"locale={profile.get('locale')}  tz={profile.get('timezone_id')}  "
        f"viewport={profile.get('viewport', {}).get('width')}x{profile.get('viewport', {}).get('height')}",
    )

    chromium_args = list(CHROMIUM_ARGS_BASE) + [f"--lang={profile.get('locale', 'en-US')}"]

    async with async_playwright() as pw:
        browser: Browser = await pw.chromium.launch(
            headless=HEADLESS,
            args=chromium_args,
        )
        context: BrowserContext = await browser.new_context(
            user_agent=profile.get("user_agent", _DEFAULT_PROFILE["user_agent"]),
            viewport=profile.get("viewport", _DEFAULT_PROFILE["viewport"]),
            locale=profile.get("locale", "en-US"),
            timezone_id=profile.get("timezone_id", "America/Los_Angeles"),
            extra_http_headers={
                "Accept-Language": profile.get("accept_language", "en-US,en;q=0.9"),
            },
        )

        page: Page = await context.new_page()
        page.set_default_timeout(STEP_TIMEOUT_MS)
        page.set_default_navigation_timeout(NAV_TIMEOUT_MS)

        async def on_response(resp):
            """Intercept Kiro's ExchangeToken.

            Two things to grab:
              1. CBOR body → accessToken, csrfToken, expiresIn, profileArn
              2. Set-Cookie: RefreshToken=<value>; HttpOnly  ← refresh token lives HERE,
                 not in the body. Kiro/Cognito stores it as an HttpOnly cookie so JS
                 can never touch it directly.
            """
            try:
                if "KiroWebPortalService/operation/ExchangeToken" not in resp.url:
                    return
                body = await resp.body()
                decoded = await _decode_cbor_response(body)
                if decoded and isinstance(decoded, dict) and decoded.get("accessToken"):
                    captured["exchange"] = decoded
                    log(email, f"🎯 intercepted ExchangeToken body ({len(body)} bytes)")

                # Pull RefreshToken from Set-Cookie headers. Playwright exposes
                # response headers as a dict where duplicate keys are joined by '\n'.
                try:
                    headers_all = await resp.all_headers()
                except Exception:
                    headers_all = resp.headers
                raw_set_cookie = headers_all.get("set-cookie") or headers_all.get("Set-Cookie") or ""
                for cookie_line in raw_set_cookie.split("\n"):
                    cookie_line = cookie_line.strip()
                    if cookie_line.startswith("RefreshToken="):
                        value = cookie_line.split(";", 1)[0][len("RefreshToken="):]
                        if value:
                            captured["refresh_token"] = value
                            log(email, f"🔑 captured RefreshToken from Set-Cookie ({len(value)} chars)")
                        break
            except Exception:
                pass

        page.on("response", on_response)

        try:
            # ── Step 1: open Kiro app root. It redirects to login automatically. ──
            log(email, "→ opening https://app.kiro.dev/")
            await page.goto(KIRO_LOGIN_URL, wait_until="domcontentloaded")
            # Nudge the SPA: wait for network idle OR a reasonable cap, whichever
            # comes first. Fixed sleeps break on slow connections, so we race.
            try:
                await page.wait_for_load_state("networkidle", timeout=8_000)
            except Exception:
                pass
            await _dismiss_cookie_banner(page)

            # ── Step 2: click "Continue with Google" (or equivalent). ──
            log(email, f"→ looking for Google sign-in button (up to {BUTTON_WAIT_SECONDS}s)")
            google_btn = await _find_google_button(page, max_wait_s=BUTTON_WAIT_SECONDS)
            if google_btn is None:
                if "account" in page.url and "signin" not in page.url:
                    log(email, "seems already signed in — reading cookies")
                else:
                    await _safe_screenshot(
                        page, SCREENSHOT_DIR / f"no_google_btn_{safe_slug(email)}.png"
                    )
                    # Dump page title + visible text snippet to make remote debugging sane.
                    try:
                        title = await page.title()
                        snippet = await page.evaluate(
                            "() => (document.body ? document.body.innerText : '').slice(0, 300)"
                        )
                        log(email, f"   page title: {title!r}")
                        log(email, f"   page text : {snippet!r}")
                    except Exception:
                        pass
                    raise RuntimeError("Google sign-in button not found")
            else:
                async with page.expect_navigation(
                    url=lambda u: "accounts.google.com" in u,
                    wait_until="domcontentloaded",
                    timeout=NAV_TIMEOUT_MS,
                ):
                    await google_btn.click()
                log(email, "→ landed on accounts.google.com")

            # ── Step 3: email field. ──
            await _raise_if_blocked(page)
            log(email, "→ typing email")
            await page.locator('input[type="email"]').first.fill(email)
            await page.wait_for_timeout(300)
            await page.locator('#identifierNext, button:has-text("Next")').first.click()
            await page.wait_for_timeout(1500)
            await _raise_if_blocked(page)

            # ── Step 4: password field. ──
            log(email, "→ typing password")
            await page.locator('input[type="password"]').first.wait_for(
                state="visible", timeout=STEP_TIMEOUT_MS
            )
            await page.locator('input[type="password"]').first.fill(password)
            await page.wait_for_timeout(300)
            await page.locator('#passwordNext, button:has-text("Next")').first.click()

            # ── Step 5: consent / allow screen (may or may not appear). ──
            log(email, "→ waiting for consent or redirect back to Kiro")
            await _click_consent_if_present(page)

            # ── Step 6: wait for the ExchangeToken interception. ──
            log(email, f"→ waiting up to {TOKEN_WAIT_SECONDS}s for Kiro ExchangeToken")
            deadline = time.monotonic() + TOKEN_WAIT_SECONDS
            while time.monotonic() < deadline:
                if captured["exchange"]:
                    break
                # If we already hit the dashboard but never saw ExchangeToken, poke it.
                if "app.kiro.dev" in page.url and "signin" not in page.url:
                    pass
                await asyncio.sleep(0.5)
                # Periodically check for Google block screens still hanging.
                if "accounts.google.com" in page.url:
                    await _raise_if_blocked(page)
                    await _click_consent_if_present(page)

            if not captured["exchange"]:
                await _safe_screenshot(
                                page, SCREENSHOT_DIR / f"no_token_{safe_slug(email)}.png"
                            )
                raise RuntimeError("ExchangeToken never fired within timeout")

            exch = captured["exchange"]
            access_token = exch["accessToken"]
            csrf_token = exch.get("csrfToken", "")

            # ── Step 7: pull UserId + VisitorId + RefreshToken from cookies. ──
            cookies = {
                c.get("name", ""): c.get("value", "")
                for c in await context.cookies()
                if c.get("name")
            }
            user_id = cookies.get("UserId", "")
            visitor_id = cookies.get("kiro-visitor-id", "")
            if not user_id or not visitor_id:
                await page.wait_for_timeout(1500)
                cookies = {
                    c.get("name", ""): c.get("value", "")
                    for c in await context.cookies()
                    if c.get("name")
                }
                user_id = cookies.get("UserId", user_id)
                visitor_id = cookies.get("kiro-visitor-id", visitor_id)

            # RefreshToken priority:
            #   1. Captured from Set-Cookie in on_response (most reliable, raw server value)
            #   2. Browser cookie jar fallback (should also contain it after redirect)
            refresh_token = captured.get("refresh_token") or cookies.get("RefreshToken", "")

            if not access_token or not user_id or not visitor_id:
                raise RuntimeError(
                    f"missing identity parts: "
                    f"token={bool(access_token)} user={bool(user_id)} visitor={bool(visitor_id)}"
                )

            log(email, f"✅ harvested — user={user_id[:18]}… visitor={visitor_id}")
            return KiroSession(
                access_token=access_token,
                refresh_token=refresh_token,
                csrf_token=csrf_token,
                user_id=user_id,
                visitor_id=visitor_id,
                cookies=cookies,
                profile=profile,
            )

        except PlaywrightTimeoutError as e:
            await _safe_screenshot(
                            page, SCREENSHOT_DIR / f"timeout_{safe_slug(email)}.png"
                        )
            raise RuntimeError(f"Playwright timeout: {e}") from e
        except GoogleBlockedError as e:
            await _safe_screenshot(
                            page, SCREENSHOT_DIR / f"google_blocked_{safe_slug(email)}.png"
                        )
            raise
        except Exception:
            try:
                await _safe_screenshot(
                                page, SCREENSHOT_DIR / f"error_{safe_slug(email)}.png"
                            )
            except Exception:
                pass
            raise
        finally:
            # Close FAST — per the spec, we don't waste time once we have the token.
            try:
                await context.close()
            except Exception:
                pass
            try:
                await browser.close()
            except Exception:
                pass


async def _safe_screenshot(page: Page, path: Path) -> None:
    """Screenshot with a hard 5s cap. Never raises, never hangs.

    The default page.screenshot() inherits STEP_TIMEOUT_MS (30s). When the page
    is wedged (common failure mode), the screenshot itself hangs, so the error
    handler ends up taking 25–30s on top of the original failure — giving
    misleading double-timeout log output. Capping to 5s keeps failures fast.
    """
    try:
        await asyncio.wait_for(
            page.screenshot(path=str(path), timeout=5_000),
            timeout=6.0,
        )
    except Exception:
        pass


async def _dismiss_cookie_banner(page: Page) -> None:
    """Some regions (EU/UK) get a GDPR consent banner that overlays the login button."""
    for sel in (
        'button:has-text("Accept all")',
        'button:has-text("Accept All")',
        'button:has-text("I agree")',
        'button:has-text("Got it")',
        'button:has-text("Setuju")',
        'button:has-text("Terima semua")',
        '[aria-label*="accept" i]',
        '[id*="cookie" i] button',
        '[class*="cookie" i] button:has-text("Accept")',
    ):
        try:
            btn = page.locator(sel).first
            if await btn.is_visible(timeout=500):
                await btn.click(timeout=2_000)
                await page.wait_for_timeout(400)
                return
        except Exception:
            continue


async def _find_google_button(page: Page, max_wait_s: int = 30):
    """Poll for the Google sign-in button, tolerant of slow React hydration.

    Kiro is a React SPA — the button is not in the initial HTML, it's injected
    after the JS bundle executes. On slow connections this can take 10–20s,
    which is why a fixed wait_for_timeout(800) + single selector lookup fails
    intermittently. We poll for up to max_wait_s against a wide selector set
    covering EN / ID / generic fallbacks.
    """
    selectors = [
        'button:has-text("Continue with Google")',
        'button:has-text("Sign in with Google")',
        'button:has-text("Log in with Google")',
        'a:has-text("Continue with Google")',
        'a:has-text("Sign in with Google")',
        'button:has-text("Lanjutkan dengan Google")',
        'button:has-text("Masuk dengan Google")',
        'button:has-text("Se connecter avec Google")',
        'button:has-text("Iniciar sesión con Google")',
        '[data-testid*="google" i]',
        '[data-test*="google" i]',
        '[aria-label*="Google" i]',
        'button >> text=/google/i',
        'a >> text=/google/i',
        '[role="button"] >> text=/google/i',
    ]
    deadline = time.monotonic() + max_wait_s
    attempt = 0
    while time.monotonic() < deadline:
        attempt += 1
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                if await loc.is_visible(timeout=300):
                    return loc
            except Exception:
                continue
        await _dismiss_cookie_banner(page)
        await page.wait_for_timeout(800)
    return None


async def _click_consent_if_present(page: Page) -> None:
    """Google shows a 'Continue' / 'Allow' screen on first-time OAuth grants.

    Covers EN / ID / ES / FR text variants since Google serves localised consent
    pages based on the account's preferred language, which ignores our UA hint.
    """
    for _ in range(3):
        if "accounts.google.com" not in page.url:
            return
        clicked = False
        for sel in [
            'button:has-text("Continue")',
            'button:has-text("Allow")',
            'button:has-text("Lanjutkan")',
            'button:has-text("Izinkan")',
            'button:has-text("Continuar")',
            'button:has-text("Permitir")',
            'button:has-text("Autoriser")',
            '[role="button"]:has-text("Continue")',
            '[role="button"]:has-text("Lanjutkan")',
        ]:
            try:
                btn = page.locator(sel).first
                if await btn.is_visible(timeout=1_000):
                    await btn.click(timeout=3_000)
                    await page.wait_for_timeout(1_200)
                    clicked = True
                    break
            except Exception:
                continue
        if not clicked:
            return


# ──────────────────────────────────────────────────────────────────────────────
# PHASE 2 — STRIPE LINK GENERATOR (PURE API)
# ──────────────────────────────────────────────────────────────────────────────


def _build_kiro_headers(session: KiroSession) -> dict[str, str]:
    """Exactly what the browser sends — smithy-protocol + rpc-v2-cbor is non-negotiable.

    UA + Accept-Language mirror the harvest profile so Phase 2's API call looks
    like it's from the same browser that just did the OAuth flow. Mismatched
    UA between phases is the fastest way to get a soft block from Kiro.
    """
    profile = session.profile or _DEFAULT_PROFILE
    platform = profile.get("platform", "macOS")
    ua_platform_hint = {
        "Windows": "Windows",
        "macOS": "macOS",
        "Linux": "Linux",
        "Chrome OS": "CrOS",
    }.get(platform, "macOS")
    return {
        "Host": "app.kiro.dev",
        "accept": "application/cbor",
        "content-type": "application/cbor",
        "smithy-protocol": "rpc-v2-cbor",
        "authorization": f"Bearer {session.access_token}",
        "x-csrf-token": session.csrf_token,
        "x-kiro-userid": session.user_id,
        "x-kiro-visitorid": session.visitor_id,
        "x-amz-user-agent": (
            f"aws-sdk-js/1.0.0 ua/2.1 os/{ua_platform_hint} "
            f"lang/js md/browser#Chromium_147 m/N,M,E"
        ),
        "amz-sdk-request": "attempt=1; max=1",
        "origin": "https://app.kiro.dev",
        "referer": "https://app.kiro.dev/account/usage",
        "user-agent": profile.get("user_agent", _DEFAULT_PROFILE["user_agent"]),
        "accept-language": profile.get("accept_language", "en-US,en;q=0.9"),
        "sec-ch-ua": profile.get("sec_ch_ua", _DEFAULT_PROFILE["sec_ch_ua"]),
        "sec-ch-ua-mobile": profile.get("sec_ch_ua_mobile", "?0"),
        "sec-ch-ua-platform": profile.get("sec_ch_ua_platform", '"macOS"'),
    }


def _extract_stripe_url(decoded: dict) -> Optional[str]:
    """Kiro returns the URL percent-encoded after the '#fid' fragment."""
    if not isinstance(decoded, dict):
        return None
    url = decoded.get("encodedVerificationUrl") or decoded.get("subscriptionManagementUrl") or decoded.get("url")
    if not url:
        # Some tenants wrap it: {"result": {"encodedVerificationUrl": "..."}}
        for v in decoded.values():
            if isinstance(v, dict):
                found = _extract_stripe_url(v)
                if found:
                    return found
        return None
    # Kiro sometimes ships the fragment already percent-encoded — leave it alone,
    # Stripe handles both. But if it's double-encoded, decode once.
    return unquote(url) if "%2F" in url and "checkout.stripe.com" in url else url


async def generate_stripe_link(email: str, session: KiroSession) -> str:
    """Pure-API hit: POST CBOR → get Stripe URL back.

    Schema (reverse-engineered from Kiro main.js Smithy codegen):
        statusOnly:       bool    (false = create new checkout session)
        provider:         string  ("Stripe")
        subscriptionType: string  (Q_DEVELOPER_STANDALONE_{PRO|PRO_PLUS|POWER})
        csrfToken:        string  (sensitive, mirrors the x-csrf-token header)
    """
    payload = {
        "statusOnly": False,
        "provider": "STRIPE",
        "subscriptionType": Q_SUBSCRIPTION_TYPE,
        "csrfToken": session.csrf_token,
    }
    body = cbor2.dumps(payload)
    headers = _build_kiro_headers(session)

    cookie_header = "; ".join(f"{k}={v}" for k, v in session.cookies.items())
    if cookie_header:
        headers["cookie"] = cookie_header

    async with httpx.AsyncClient(
        http2=False, timeout=30.0, follow_redirects=False
    ) as client:
        log(email, f"→ POST {KIRO_SUB_ENDPOINT} ({len(body)} bytes CBOR)")
        resp = await client.post(KIRO_SUB_ENDPOINT, content=body, headers=headers)

    if resp.status_code != 200:
        snippet = resp.text[:200] if resp.text else resp.content[:200]
        raise RuntimeError(f"kiro_api_status_{resp.status_code}: {snippet!r}")

    try:
        decoded = cbor2.loads(resp.content)
    except Exception as e:
        raise RuntimeError(f"cbor_decode_failed: {e}") from e

    url = _extract_stripe_url(decoded)
    if not url or "checkout.stripe.com" not in url:
        raise RuntimeError(f"no_stripe_url_in_response: {str(decoded)[:200]}")

    log(email, f"💳 Stripe URL acquired ({len(url)} chars)")
    return url


# ──────────────────────────────────────────────────────────────────────────────
# ORCHESTRATION
# ──────────────────────────────────────────────────────────────────────────────


def _classify_error(exc: BaseException) -> str:
    """Map exception to a short, grep-friendly tag for hybrid_links.txt."""
    if isinstance(exc, GoogleBlockedError):
        return f"ERROR_GoogleBlocked_{str(exc)[:40].replace(':', '_').replace(' ', '_')}"
    if isinstance(exc, PlaywrightTimeoutError):
        return "ERROR_PlaywrightTimeout"
    msg = str(exc) or exc.__class__.__name__
    msg = msg.replace(":", "_").replace(" ", "_").replace("\n", "_")[:80]
    return f"ERROR_{msg}"


_RETRYABLE_PHASE1_TAGS = (
    "ERROR_PlaywrightTimeout",
    "ERROR_OuterTimeout",
    "ERROR_Google_sign-in_button_not_found",
    "ERROR_ExchangeToken_never_fired_within_timeout",
    "ERROR_missing_identity_parts",
)


def _is_retryable_phase1(tag: str) -> bool:
    """Transient failures worth retrying once. Hard failures (wrong password,
    Google blocked, account cooked) are NOT retried — they'll just waste time.
    """
    if tag.startswith("ERROR_GoogleBlocked"):
        return False
    return any(tag.startswith(t) for t in _RETRYABLE_PHASE1_TAGS)


async def _run_phase1(email: str, password: str) -> KiroSession:
    return await asyncio.wait_for(
        harvest_tokens(email, password),
        timeout=NAV_TIMEOUT_MS / 1000 + TOKEN_WAIT_SECONDS + 30,
    )


async def process_account(email: str, password: str) -> AccountResult:
    """Run both phases for one account. NEVER re-raises — always returns a result."""
    session: Optional[KiroSession] = None
    last_tag = "ERROR_Unknown"
    for attempt in range(1, PHASE1_RETRIES + 2):
        try:
            session = await _run_phase1(email, password)
            if session.refresh_token:
                try:
                    with REFRESH_TOKEN_FILE.open("a", encoding="utf-8") as f:
                        f.write(f"{email}:{session.refresh_token}\n")
                except Exception as e:
                    log(email, f"⚠️ failed to save refresh token: {e}")
            break
        except asyncio.TimeoutError:
            last_tag = "ERROR_OuterTimeout"
            log(email, f"✗ outer timeout during harvest (attempt {attempt})")
        except Exception as e:
            last_tag = _classify_error(e)
            log(email, f"✗ harvest failed (attempt {attempt}): {e.__class__.__name__}: {e}")
        if attempt <= PHASE1_RETRIES and _is_retryable_phase1(last_tag):
            log(email, f"↻ retrying phase 1 ({attempt}/{PHASE1_RETRIES})…")
            await asyncio.sleep(2.0)
            continue
        return AccountResult(email, last_tag, last_tag)

    if session is None:
        return AccountResult(email, last_tag, last_tag)

    # PHASE 2 — Stripe Link Generator
    try:
        url = await asyncio.wait_for(generate_stripe_link(email, session), timeout=60)
    except asyncio.TimeoutError:
        log(email, "✗ Stripe API timeout")
        return AccountResult(email, "ERROR_StripeApiTimeout", "ERROR_StripeApiTimeout")
    except Exception as e:
        log(email, f"✗ Stripe API failed: {e.__class__.__name__}: {e}")
        tag = _classify_error(e)
        return AccountResult(email, tag, tag)

    return AccountResult(email, "OK", url)


async def run_all(accounts: list[tuple[str, str]]) -> None:
    sem = asyncio.Semaphore(CONCURRENCY)

    async def bounded(email: str, password: str):
        async with sem:
            return await process_account(email, password)

    for idx, (email, password) in enumerate(accounts, 1):
        print(f"\n{'═' * 78}")
        print(f"  [{idx}/{len(accounts)}]  {email}")
        print(f"{'═' * 78}")
        try:
            result = await bounded(email, password)
        except Exception as e:
            # Absolute safety net — bounded() already swallows everything, but just in case.
            log(email, f"‼️  unhandled: {e!r}\n{traceback.format_exc()}")
            result = AccountResult(email, "ERROR_UnhandledException", "ERROR_UnhandledException")
        append_result(result)
        print(f"  → wrote: {result.email}:{result.payload[:90]}{'…' if len(result.payload) > 90 else ''}")


def main() -> None:
    SCREENSHOT_DIR.mkdir(exist_ok=True)

    try:
        accounts = read_accounts(INPUT_FILE)
    except FileNotFoundError as e:
        print(f"FATAL: {e}")
        sys.exit(1)

    if not accounts:
        print("FATAL: no accounts parsed from email.txt")
        sys.exit(1)

    print(f"Loaded {len(accounts)} account(s) from {INPUT_FILE.name}")
    print(f"Output → {OUTPUT_FILE.name}   Screenshots → {SCREENSHOT_DIR.name}/")
    print(f"Plan: {Q_SUBSCRIPTION_TYPE}\n")

    # Fresh log each run. (GUA COMMENT BIAR HASIL LAMA GA ILANG)
    # OUTPUT_FILE.write_text("", encoding="utf-8")

    try:
        asyncio.run(run_all(accounts))
    except KeyboardInterrupt:
        print("\n⚠️  interrupted by user — partial results already flushed to hybrid_links.txt")
        sys.exit(130)

    print(f"\n✅ done — results in {OUTPUT_FILE}")


if __name__ == "__main__":
    main()

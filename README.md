# KikirRo — Kiro Hybrid Bot

Two-phase harvester + API caller for [app.kiro.dev](https://app.kiro.dev):

1. **Phase 1 (Patchright/Playwright):** log in via Google SSO, intercept the `ExchangeToken` Smithy/CBOR response, and capture `AccessToken`, `CsrfToken`, `UserId`, `VisitorId`, plus the `RefreshToken` HttpOnly cookie.
2. **Phase 2 (httpx + CBOR):** POST to `GenerateSubscriptionManagementUrl` to mint a Stripe Checkout URL for the configured plan.

## Requirements

- Python 3.10+
- [patchright](https://pypi.org/project/patchright/) (stealth Playwright fork)
- `httpx`, `cbor2`

```bash
pip install patchright httpx cbor2
patchright install chromium
```

## Usage

```bash
cp email.txt.example email.txt
# edit email.txt — one account per line, format: email:password
python3 bot_hybrid.py
```

## Outputs

| File | Content |
|------|---------|
| `hybrid_links.txt` | `email:stripe_url` on success, `email:ERROR_<reason>` on failure |
| `refresh_tokens.txt` | `email:refresh_token` for each account that completes phase 1 |
| `screenshots/` | Auto-snapshot on every failure path for debugging |

## Config

All settings are overridable via environment variables. No need to edit the source.

| Env var | Default | Purpose |
|---|---|---|
| `KIRO_PLAN` | `PRO` | `PRO` / `PRO_PLUS` / `POWER` (or full `Q_DEVELOPER_STANDALONE_*` string) |
| `KIRO_HEADLESS` | `true` | Set to `false` to watch the browser — essential for debugging |
| `KIRO_CONCURRENCY` | `1` | Parallel accounts. Raise cautiously, Google rate-limits hard |
| `KIRO_NAV_TIMEOUT_MS` | `60000` | Hard cap on any navigation. Bump on slow networks |
| `KIRO_STEP_TIMEOUT_MS` | `30000` | Hard cap on any single selector wait |
| `KIRO_TOKEN_WAIT_SECONDS` | `90` | Time window to intercept `ExchangeToken` after consent |
| `KIRO_BUTTON_WAIT_SECONDS` | `30` | Time to wait for Kiro's "Continue with Google" to render |
| `KIRO_PHASE1_RETRIES` | `1` | Auto-retry count on transient login failures |
| `KIRO_LOCALE` | `en-US` | Browser locale. Keep English — non-EN breaks selector text matching |
| `KIRO_TIMEZONE` | `America/Los_Angeles` | IANA TZ id for the browser context |
| `KIRO_ACCEPT_LANG` | `en-US,en;q=0.9` | `Accept-Language` header |
| `KIRO_USER_AGENT` | recent Chrome macOS | Override if Kiro starts blocking this UA |
| `KIRO_EMAIL_FILE` | `./email.txt` | Path to input credentials file |
| `KIRO_OUTPUT_FILE` | `./hybrid_links.txt` | Path to results file |
| `KIRO_REFRESH_FILE` | `./refresh_tokens.txt` | Path to refresh-token dump |
| `KIRO_SCREENSHOT_DIR` | `./screenshots` | Failure screenshot directory |

Examples:

```bash
# Debug visually — watch the browser
KIRO_HEADLESS=false python3 bot_hybrid.py

# Slow connection? bump timeouts
KIRO_NAV_TIMEOUT_MS=90000 KIRO_BUTTON_WAIT_SECONDS=60 python3 bot_hybrid.py

# Different plan
KIRO_PLAN=PRO_PLUS python3 bot_hybrid.py
```

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `ERROR_Google_sign-in_button_not_found` | Slow network / cookie banner / UI variant | Re-run once (auto-retry handles most). Else run with `KIRO_HEADLESS=false` to watch |
| `ERROR_ExchangeToken_never_fired...` | Consent screen stuck, Google 2FA triggered | Check `screenshots/no_token_*.png` |
| `ERROR_GoogleBlocked_*` | Account flagged (wrong password, suspicious activity, etc.) | Nothing to do — account needs manual login first |
| `ERROR_OuterTimeout` | Browser hung or Playwright zombie | Rerun. Bump `KIRO_NAV_TIMEOUT_MS` if it keeps happening |
| `ERROR_StripeApiTimeout` | Kiro backend slow | Rerun only the failed account |

Every failure writes a screenshot to `screenshots/` and logs page title + a snippet of the visible text, so you can diagnose remotely.

## Notes

- `refreshToken` is **not** in the `ExchangeToken` response body. Kiro/Cognito delivers it as an `HttpOnly` `Set-Cookie: RefreshToken=...` header on the same response. The bot parses it from the response headers.
- Google may flag the sign-in as suspicious on first use of a new account — the bot detects common block phrases (`Verify it's you`, `suspicious sign-in`, etc.) and fails fast with a screenshot.
- **Never commit `email.txt`, `refresh_tokens.txt`, `hybrid_links.txt`, `*.har`, or `screenshots/`.** They contain live credentials and tokens. `.gitignore` already blocks them.

## License

No warranty. Use responsibly. Respect Kiro/AWS/Google ToS.

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

Edit the constants at the top of `bot_hybrid.py`:

- `Q_SUBSCRIPTION_TYPE` — `Q_DEVELOPER_STANDALONE_PRO` / `_PRO_PLUS` / `_POWER`
- `CONCURRENCY` — keep at `1` unless you want Google to rate-limit your IP
- `TOKEN_WAIT_SECONDS` — max wait for `ExchangeToken` interception

## Notes

- `refreshToken` is **not** in the `ExchangeToken` response body. Kiro/Cognito delivers it as an `HttpOnly` `Set-Cookie: RefreshToken=...` header on the same response. The bot parses it from the response headers.
- Google may flag the sign-in as suspicious on first use of a new account — the bot detects common block phrases (`Verify it's you`, `suspicious sign-in`, etc.) and fails fast with a screenshot.
- **Never commit `email.txt`, `refresh_tokens.txt`, `hybrid_links.txt`, `*.har`, or `screenshots/`.** They contain live credentials and tokens. `.gitignore` already blocks them.

## License

No warranty. Use responsibly. Respect Kiro/AWS/Google ToS.

"""
WonderwallAi demo key minter.

Run with:
    python3 /Users/louisconstant/SkintLabs/assets/mint_demo_key.py

It will prompt you for your admin key (hidden input, like a password
prompt), mint a free key, then upgrade it to pro. Prints the ww_live_
key at the end so you can paste it into the dashboard login.

No shell quoting, no curl wrangling, no copy paste headaches.
"""

import getpass
import json
import ssl
import sys
import urllib.request
import urllib.error

API = "https://wonderwallai-production.up.railway.app"

# macOS Python ships without a cert bundle by default. Since we're calling
# our own backend with an admin token (already authenticated channel), we
# skip cert verification rather than make the user install certifi.
SSL_CTX = ssl._create_unverified_context()


def request(method: str, path: str, admin_key: str, body: dict | None = None) -> dict:
    url = API + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "X-Admin-API-Key": admin_key,
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30, context=SSL_CTX) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body_txt = e.read().decode()
        try:
            body_json = json.loads(body_txt)
        except Exception:
            body_json = {"raw": body_txt}
        print(f"\n✗ HTTP {e.code} on {method} {path}")
        print(f"  {body_json}")
        sys.exit(1)
    except Exception as e:
        print(f"\n✗ Error on {method} {path}: {e}")
        sys.exit(1)


def main():
    print("WonderwallAi demo key minter")
    print("─" * 40)
    admin_key = getpass.getpass("Paste your ADMIN_API_KEY (hidden): ").strip()
    if not admin_key:
        print("No key entered, exiting.")
        sys.exit(1)

    plan_choice = (input("Target plan [pro / business / starter] (default pro): ").strip()
                   or "pro").lower()

    print(f"\n→ Minting free key first (no Stripe involved) ...")
    created = request(
        "POST",
        "/admin/keys",
        admin_key,
        {
            "name": "Louis demo",
            "owner_email": "info@skintlabs.ai",
            "plan": "free",
        },
    )

    raw_key = created["api_key"]
    prefix = created["key_prefix"]
    print(f"  ✓ Key created. Prefix: {prefix}")

    print(f"\n→ Upgrading {prefix} to plan='{plan_choice}' ...")
    upgraded = request(
        "PATCH",
        f"/admin/keys/{prefix}/plan",
        admin_key,
        {"plan": plan_choice},
    )
    print(f"  ✓ Plan now: {upgraded['plan']} (rate_limit={upgraded['rate_limit']})")

    print()
    print("─" * 40)
    print("DASHBOARD LOGIN KEY:")
    print()
    print(f"   {raw_key}")
    print()
    print("Paste that into the dashboard login at")
    print("https://wonderwallai.skintlabs.ai/dashboard.html")
    print("─" * 40)


if __name__ == "__main__":
    main()

import os
import sys
import json
import webbrowser
import urllib.parse
import requests

SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")
# TikTok's sandbox/production OAuth rejects localhost redirect URIs entirely, unlike
# Meta/Google — so this points at a public endpoint on the deployed app instead, which
# displays the code on screen for you to paste back here.
REDIRECT_URI = os.environ.get(
    "TIKTOK_REDIRECT_URI",
    "https://marketing-automation-production-dbd5.up.railway.app/tiktok-callback"
)
SCOPES = "user.info.basic,video.publish,video.upload"

def main():
    print("=" * 60)
    print("         TikTok OAuth 2.0 Token Generator helper")
    print("=" * 60)

    if os.path.exists(SETTINGS_FILE):
        with open(SETTINGS_FILE, "r") as f:
            settings = json.load(f)
    else:
        settings = {}

    client_key = os.environ.get("TIKTOK_CLIENT_KEY") or settings.get("tiktok_client_key") or ""
    if not client_key:
        if len(sys.argv) > 1:
            client_key = sys.argv[1]
        else:
            client_key = input("Please paste your TikTok Client Key: ").strip()
    if not client_key:
        print("Error: Client Key is required.")
        sys.exit(1)

    client_secret = os.environ.get("TIKTOK_CLIENT_SECRET") or settings.get("tiktok_client_secret") or ""
    if not client_secret:
        if len(sys.argv) > 2:
            client_secret = sys.argv[2]
        else:
            client_secret = input("Please paste your TikTok Client Secret: ").strip()
    if not client_secret:
        print("Error: Client Secret is required.")
        sys.exit(1)

    print("\nIMPORTANT: Your TikTok app must have this EXACT redirect URI registered under")
    print(f"Login Kit > Redirect URI: {REDIRECT_URI}")
    print("Note: unaudited apps can only post videos as private (visible only to you) via")
    print("the Content Posting API — this matches this app's default privacy_level setting.")
    print("Submit your app for audit with TikTok to unlock public posting.\n")

    state = "tiktok_auth_state"
    params = {
        "client_key": client_key,
        "scope": SCOPES,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "state": state,
    }
    auth_url = "https://www.tiktok.com/v2/auth/authorize/?" + urllib.parse.urlencode(params)

    print("=" * 60)
    print("Opening your browser to log in with your TikTok account...")
    print(f"If it doesn't open automatically, visit:\n{auth_url}")
    print("After you log in and approve, the page will show an authorization code.")
    print("=" * 60)
    webbrowser.open(auth_url)

    captured_code = input("\nPaste the authorization code shown on that page here: ").strip()
    if not captured_code:
        print("Error: No code entered.")
        sys.exit(1)

    print(f"Using Auth Code: {captured_code[:10]}...")

    print("Exchanging authorization code for an access token...")
    token_res = requests.post(
        "https://open.tiktokapis.com/v2/oauth/token/",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "client_key": client_key,
            "client_secret": client_secret,
            "code": captured_code,
            "grant_type": "authorization_code",
            "redirect_uri": REDIRECT_URI,
        }
    )
    if token_res.status_code != 200:
        print(f"Error exchanging token: {token_res.status_code} - {token_res.text}")
        sys.exit(1)

    token_json = token_res.json()
    access_token = token_json.get("access_token")
    if not access_token:
        print(f"Error: No access_token in response: {token_json}")
        sys.exit(1)

    print("Successfully retrieved access token.")

    # Verify by fetching basic user info
    print("Verifying access to your TikTok account...")
    info_res = requests.get(
        "https://open.tiktokapis.com/v2/user/info/",
        headers={"Authorization": f"Bearer {access_token}"},
        params={"fields": "display_name"}
    )
    if info_res.status_code == 200:
        display_name = info_res.json().get("data", {}).get("user", {}).get("display_name")
        if display_name:
            print(f"Authenticated as: {display_name}")
    else:
        print(f"Note: Could not verify user info (HTTP {info_res.status_code}), but token was saved.")

    settings["tiktok_client_key"] = client_key
    settings["tiktok_client_secret"] = client_secret
    settings["tiktok_access_token"] = access_token
    if token_json.get("refresh_token"):
        settings["tiktok_refresh_token"] = token_json["refresh_token"]

    with open(SETTINGS_FILE, "w") as f:
        json.dump(settings, f, indent=4)

    print(f"\nSaved TikTok credentials to {SETTINGS_FILE}!")
    print("Note: TikTok access tokens expire in 24 hours; the refresh_token (valid ~365 days)")
    print("was also saved, but this app's publish flow currently uses the access_token directly —")
    print("re-run this script when it expires.")
    print("=" * 60)

if __name__ == "__main__":
    main()

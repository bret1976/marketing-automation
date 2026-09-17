import os
import sys
import json
import webbrowser
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
import requests

SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")
REDIRECT_URI = "http://localhost:8080/callback"
GRAPH_VERSION = "v21.0"
# Override with e.g. META_SCOPES="pages_show_list,pages_manage_posts" when the app
# doesn't have every permission enabled yet.
SCOPES = os.environ.get(
    "META_SCOPES",
    "pages_show_list,pages_read_engagement,pages_manage_posts,instagram_basic,instagram_content_publish,business_management"
)

captured_code = None

class CallbackHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        return

    def do_GET(self):
        global captured_code
        parsed_url = urllib.parse.urlparse(self.path)
        query_params = urllib.parse.parse_qs(parsed_url.query)

        if parsed_url.path == "/callback":
            if "code" in query_params:
                captured_code = query_params["code"][0]
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(b"""
                <html>
                <head><style>
                    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; background: #0f172a; color: #f8fafc; text-align: center; padding: 50px; }
                    h1 { color: #10b981; }
                    .card { background: #1e293b; padding: 30px; border-radius: 8px; display: inline-block; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.1); margin-top: 20px; }
                </style></head>
                <body>
                    <div class="card">
                        <h1>Authorization Successful!</h1>
                        <p>You can close this browser tab and return to the terminal to finish setup.</p>
                    </div>
                </body>
                </html>
                """)
            else:
                error_msg = query_params.get("error_description", ["Unknown error"])[0]
                self.send_response(400)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(f"""
                <html>
                <head><style>
                    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; background: #0f172a; color: #f8fafc; text-align: center; padding: 50px; }}
                    h1 {{ color: #ef4444; }}
                </style></head>
                <body>
                    <h1>Authorization Failed!</h1>
                    <p>Error: {error_msg}</p>
                </body>
                </html>
                """.encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()

def main():
    print("=" * 60)
    print("    Meta (Instagram + Facebook) OAuth 2.0 Token Generator helper")
    print("=" * 60)

    if os.path.exists(SETTINGS_FILE):
        with open(SETTINGS_FILE, "r") as f:
            settings = json.load(f)
    else:
        settings = {}

    app_id = os.environ.get("META_APP_ID") or settings.get("meta_app_id") or ""
    if not app_id:
        if len(sys.argv) > 1:
            app_id = sys.argv[1]
        else:
            app_id = input("Please paste your Meta App ID: ").strip()
    if not app_id:
        print("Error: App ID is required.")
        sys.exit(1)

    app_secret = os.environ.get("META_APP_SECRET") or settings.get("meta_app_secret") or ""
    if not app_secret:
        if len(sys.argv) > 2:
            app_secret = sys.argv[2]
        else:
            app_secret = input("Please paste your Meta App Secret: ").strip()
    if not app_secret:
        print("Error: App Secret is required.")
        sys.exit(1)

    print("\nIMPORTANT: Your Meta App must have this EXACT redirect URI registered under")
    print(f"Facebook Login > Settings > Valid OAuth Redirect URIs: {REDIRECT_URI}")
    print("Your app must also request the scopes below — if it's still in Development mode,")
    print("only accounts added as Admins/Developers/Testers on the app can authorize it,")
    print("or you'll need App Review for instagram_content_publish / pages_manage_posts")
    print("before publishing works for other accounts.\n")

    params = {
        "client_id": app_id,
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPES,
        "response_type": "code",
    }
    auth_url = f"https://www.facebook.com/{GRAPH_VERSION}/dialog/oauth?" + urllib.parse.urlencode(params)

    server = HTTPServer(("localhost", 8080), CallbackHandler)

    print("=" * 60)
    print("Opening your browser to log in with the Facebook account that manages your Page...")
    print(f"If it doesn't open automatically, visit:\n{auth_url}")
    print("=" * 60)
    webbrowser.open(auth_url)

    try:
        while captured_code is None:
            server.handle_request()
    except KeyboardInterrupt:
        print("\nAborted by user.")
        sys.exit(1)

    print(f"Captured Auth Code: {captured_code[:10]}...")

    # Exchange code for a short-lived user access token
    print("Exchanging authorization code for a user access token...")
    token_res = requests.get(f"https://graph.facebook.com/{GRAPH_VERSION}/oauth/access_token", params={
        "client_id": app_id,
        "client_secret": app_secret,
        "redirect_uri": REDIRECT_URI,
        "code": captured_code,
    })
    if token_res.status_code != 200:
        print(f"Error exchanging token: {token_res.status_code} - {token_res.text}")
        sys.exit(1)
    short_lived_token = token_res.json().get("access_token")
    if not short_lived_token:
        print("Error: No access_token in response.")
        sys.exit(1)

    # Exchange for a long-lived user access token (~60 days)
    print("Exchanging for a long-lived user access token...")
    exchange_res = requests.get(f"https://graph.facebook.com/{GRAPH_VERSION}/oauth/access_token", params={
        "grant_type": "fb_exchange_token",
        "client_id": app_id,
        "client_secret": app_secret,
        "fb_exchange_token": short_lived_token,
    })
    if exchange_res.status_code == 200 and exchange_res.json().get("access_token"):
        user_token = exchange_res.json()["access_token"]
        print("Successfully obtained long-lived user token.")
    else:
        print("Warning: Long-lived token exchange failed, continuing with short-lived token.")
        user_token = short_lived_token

    # Discover Pages this user manages (each includes its own Page Access Token,
    # which is long-lived when derived from a long-lived user token)
    print("Fetching Pages you manage...")
    pages_res = requests.get(f"https://graph.facebook.com/{GRAPH_VERSION}/me/accounts", params={
        "access_token": user_token,
        "fields": "id,name,access_token",
    })
    if pages_res.status_code != 200:
        print(f"Error fetching Pages: {pages_res.status_code} - {pages_res.text}")
        sys.exit(1)
    pages = pages_res.json().get("data", [])
    if not pages:
        print("\nNo Facebook Pages found for this account. You need to be an admin of at least")
        print("one Facebook Page to publish. Create one at facebook.com/pages/create, link it,")
        print("then re-run this script.")
        sys.exit(1)

    print("\n" + "=" * 60)
    print("Select which Page to publish to:")
    print("=" * 60)
    for idx, page in enumerate(pages):
        print(f"[{idx + 1}] {page['name']} (ID: {page['id']})")

    selected_page = None
    while True:
        try:
            sel_str = input(f"\nEnter selection [1-{len(pages)}] (Default: 1): ").strip()
            sel_idx = int(sel_str) - 1 if sel_str else 0
            if 0 <= sel_idx < len(pages):
                selected_page = pages[sel_idx]
                break
            print(f"Invalid option. Please enter a number between 1 and {len(pages)}.")
        except ValueError:
            print("Invalid input. Please enter a number.")

    page_id = selected_page["id"]
    page_access_token = selected_page["access_token"]
    print(f"\nSelected Page: {selected_page['name']} ({page_id})")

    # Look up the Instagram Business Account linked to this Page, if any
    print("Checking for a linked Instagram Business/Creator account...")
    ig_res = requests.get(f"https://graph.facebook.com/{GRAPH_VERSION}/{page_id}", params={
        "fields": "instagram_business_account",
        "access_token": page_access_token,
    })
    instagram_account_id = None
    if ig_res.status_code == 200:
        instagram_account_id = ig_res.json().get("instagram_business_account", {}).get("id")

    if instagram_account_id:
        print(f"Found linked Instagram Business Account: {instagram_account_id}")
    else:
        print("No Instagram Business Account is linked to this Page yet. Facebook publishing will")
        print("still work; to enable Instagram too, link an Instagram Professional account to this")
        print("Page in the Meta Business Suite, then re-run this script.")

    # Save to settings.json
    settings["meta_app_id"] = app_id
    settings["meta_app_secret"] = app_secret
    settings["facebook_page_id"] = page_id
    settings["facebook_page_access_token"] = page_access_token
    if instagram_account_id:
        settings["instagram_business_account_id"] = instagram_account_id
        settings["instagram_access_token"] = page_access_token  # Instagram Graph API publishing uses the Page token

    with open(SETTINGS_FILE, "w") as f:
        json.dump(settings, f, indent=4)

    print(f"\nSaved Meta credentials to {SETTINGS_FILE}!")
    print("Note: Page Access Tokens derived this way are long-lived (~60 days) but do expire.")
    print("Re-run this script periodically to refresh it.")
    print("=" * 60)

if __name__ == "__main__":
    main()

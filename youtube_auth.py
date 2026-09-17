import os
import sys
import json
import webbrowser
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
import requests

SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")
REDIRECT_URI = "http://localhost:8080/callback"
SCOPE = " ".join([
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.force-ssl",
    "https://www.googleapis.com/auth/youtube.readonly",
])

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
    print("         YouTube (Google) OAuth 2.0 Token Generator helper")
    print("=" * 60)

    if os.path.exists(SETTINGS_FILE):
        with open(SETTINGS_FILE, "r") as f:
            settings = json.load(f)
    else:
        settings = {}

    # 1. Get Client ID
    client_id = os.environ.get("YOUTUBE_CLIENT_ID") or settings.get("youtube_client_id") or ""
    if not client_id:
        if len(sys.argv) > 1:
            client_id = sys.argv[1]
        else:
            client_id = input("Please paste your Google Cloud OAuth Client ID: ").strip()
    if not client_id:
        print("Error: Client ID is required.")
        sys.exit(1)

    # 2. Get Client Secret
    client_secret = os.environ.get("YOUTUBE_CLIENT_SECRET") or settings.get("youtube_client_secret") or ""
    if not client_secret:
        if len(sys.argv) > 2:
            client_secret = sys.argv[2]
        else:
            client_secret = input("Please paste your Google Cloud OAuth Client Secret: ").strip()
    if not client_secret:
        print("Error: Client Secret is required.")
        sys.exit(1)

    print("\nIMPORTANT: Your Google Cloud OAuth Client must have this EXACT redirect URI")
    print(f"registered under 'Authorized redirect URIs': {REDIRECT_URI}")
    print("If it's not there yet, add it in Google Cloud Console > APIs & Services > Credentials,")
    print("then re-run this script.\n")

    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPE,
        "access_type": "offline",
        "prompt": "consent",  # forces a refresh_token to be issued even on repeat authorizations
    }
    auth_url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(params)

    server = HTTPServer(("localhost", 8080), CallbackHandler)

    print("=" * 60)
    print("Opening your browser to sign in with the Google account that owns your YouTube channel...")
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

    # 3. Exchange code for tokens
    print("Exchanging authorization code for tokens...")
    token_url = "https://oauth2.googleapis.com/token"
    token_data = {
        "grant_type": "authorization_code",
        "code": captured_code,
        "redirect_uri": REDIRECT_URI,
        "client_id": client_id,
        "client_secret": client_secret,
    }
    res = requests.post(token_url, data=token_data)
    if res.status_code != 200:
        print(f"Error exchanging token: {res.status_code} - {res.text}")
        sys.exit(1)

    token_json = res.json()
    refresh_token = token_json.get("refresh_token")
    access_token = token_json.get("access_token")
    if not refresh_token:
        print("\nWarning: No refresh_token was returned. This usually means you've already granted")
        print("consent before and Google didn't re-issue one. Revoke prior access at")
        print("https://myaccount.google.com/permissions and re-run this script.")
        sys.exit(1)

    print("Successfully retrieved refresh token.")

    # 4. Verify by fetching the channel this token can upload to
    print("Verifying access to your YouTube channel...")
    channel_res = requests.get(
        "https://www.googleapis.com/youtube/v3/channels",
        params={"part": "snippet", "mine": "true"},
        headers={"Authorization": f"Bearer {access_token}"}
    )
    if channel_res.status_code == 200:
        items = channel_res.json().get("items", [])
        if items:
            print(f"Authenticated as channel: {items[0]['snippet']['title']}")
        else:
            print("Warning: Token is valid but no YouTube channel was found on this Google account.")
    else:
        print(f"Note: Could not verify channel (HTTP {channel_res.status_code}), but tokens were saved.")

    # 5. Save to settings.json
    settings["youtube_client_id"] = client_id
    settings["youtube_client_secret"] = client_secret
    settings["youtube_refresh_token"] = refresh_token

    with open(SETTINGS_FILE, "w") as f:
        json.dump(settings, f, indent=4)

    print(f"\nSaved YouTube credentials to {SETTINGS_FILE}!")
    print("=" * 60)

if __name__ == "__main__":
    main()

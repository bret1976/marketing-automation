import os
import sys
import json
import webbrowser
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
import requests

SETTINGS_FILE = "/Users/majid/.gemini/antigravity/scratch/marketing-automation/settings.json"
CLIENT_ID = "86qxw9dxdmct8y"
REDIRECT_URI = "http://localhost:8080/callback"

# Global variable to store captured code
captured_code = None

class CallbackHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Suppress logging HTTP requests to clean up terminal output
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
    print("         LinkedIn OAuth 2.0 Token Generator helper")
    print("=" * 60)

    # 1. Load settings to ensure file exists and read existing values
    if os.path.exists(SETTINGS_FILE):
        with open(SETTINGS_FILE, "r") as f:
            settings = json.load(f)
    else:
        settings = {}

    # 2. Get Client Secret
    client_secret = os.environ.get("LINKEDIN_CLIENT_SECRET")
    if not client_secret:
        if len(sys.argv) > 1:
            client_secret = sys.argv[1]
        else:
            client_secret = input("Please paste your LinkedIn Primary Client Secret: ").strip()

    if not client_secret:
        print("Error: Client Secret is required.")
        sys.exit(1)

    # 3. Generate authorization URLs for different scope configurations
    state = "random_state_string_123"
    
    # Configuration 1: Full Scopes (Sign In, Share, Company Page)
    params_full = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "state": state,
        "scope": "openid profile w_member_social w_organization_social"
    }
    url_full = "https://www.linkedin.com/oauth/v2/authorization?" + urllib.parse.urlencode(params_full)

    # Configuration 2: Company Page ONLY (requires only Community Management API product)
    params_org_only = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "state": state,
        "scope": "w_organization_social"
    }
    url_org_only = "https://www.linkedin.com/oauth/v2/authorization?" + urllib.parse.urlencode(params_org_only)

    # Configuration 3: Company Page + OIDC (requires Community Management API + Sign In OIDC)
    params_org_oidc = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "state": state,
        "scope": "openid profile w_organization_social"
    }
    url_org_oidc = "https://www.linkedin.com/oauth/v2/authorization?" + urllib.parse.urlencode(params_org_oidc)

    # Configuration 4: Personal Feed ONLY (requires Sign In OIDC + Share on LinkedIn)
    params_personal = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "state": state,
        "scope": "openid profile w_member_social"
    }
    url_personal = "https://www.linkedin.com/oauth/v2/authorization?" + urllib.parse.urlencode(params_personal)

    # 4. Start local server
    server = HTTPServer(("localhost", 8080), CallbackHandler)
    
    print("\n" + "=" * 60)
    print("Please click one of the following URLs based on your enabled products:")
    print("=" * 60)
    print(f"\n[Option A] All Scopes (Sign In + Share + Company Page):\n{url_full}")
    print(f"\n[Option B] Company Page Posting ONLY (requires ONLY 'Community Management API'):\n{url_org_only}")
    print(f"\n[Option C] Company Page + Profile Name (requires 'Community Management API' & 'Sign In'):\n{url_org_oidc}")
    print(f"\n[Option D] Personal Feed Posting ONLY (requires 'Share' & 'Sign In'):\n{url_personal}")
    print("\n" + "=" * 60)
    print("Opening browser with Option A (All Scopes) by default. If it fails with a scope error, close it and click Option B or Option C above instead.")
    webbrowser.open(url_full)

    try:
        # Wait for callback
        while captured_code is None:
            server.handle_request()
    except KeyboardInterrupt:
        print("\nAborted by user.")
        sys.exit(1)

    print(f"Captured Auth Code: {captured_code[:10]}...")

    # 5. Exchange code for Access Token
    print("Exchanging authorization code for Access Token...")
    token_url = "https://www.linkedin.com/oauth/v2/accessToken"
    token_data = {
        "grant_type": "authorization_code",
        "code": captured_code,
        "redirect_uri": REDIRECT_URI,
        "client_id": CLIENT_ID,
        "client_secret": client_secret
    }
    
    res = requests.post(token_url, data=token_data)
    if res.status_code != 200:
        print(f"Error exchanging token: {res.status_code} - {res.text}")
        sys.exit(1)

    token_json = res.json()
    access_token = token_json.get("access_token")
    if not access_token:
        print("Error: No access_token in response.")
        sys.exit(1)

    print("Successfully retrieved Access Token.")

    # 6. Retrieve User Profile (OpenID Connect /v2/userinfo)
    print("Retrieving LinkedIn profile details...")
    userinfo_url = "https://api.linkedin.com/v2/userinfo"
    headers = {
        "Authorization": f"Bearer {access_token}"
    }
    
    profile_res = requests.get(userinfo_url, headers=headers)
    person_urn_id = None
    name = "LinkedIn User"

    if profile_res.status_code == 200:
        profile_json = profile_res.json()
        person_urn_id = profile_json.get("sub")
        name = profile_json.get("name", "LinkedIn User")
    else:
        # Fallback to legacy /v2/me if openid is not fully working or scopes differ
        print("OIDC userinfo failed, trying fallback legacy /v2/me endpoint...")
        me_res = requests.get("https://api.linkedin.com/v2/me", headers=headers)
        if me_res.status_code == 200:
            me_json = me_res.json()
            person_urn_id = me_json.get("id")
            name = f"{me_json.get('localizedFirstName', '')} {me_json.get('localizedLastName', '')}".strip() or "LinkedIn User"
        else:
            print(f"Warning: Could not fetch profile details (Userinfo: {profile_res.status_code}, /v2/me: {me_res.status_code}). Skipping profile details.")
            person_urn_id = None
            name = "LinkedIn User"

    if person_urn_id:
        print(f"Successfully authenticated as: {name} ({person_urn_id})")
    else:
        print("Continuing with profile details set to unknown (Personal posting option will be unavailable).")

    # Fetch organizations you administer
    print("Attempting to fetch LinkedIn pages you administer...")
    headers_restli = {
        "Authorization": f"Bearer {access_token}",
        "X-Restli-Protocol-Version": "2.0.0"
    }
    
    companies = []
    try:
        acl_url = "https://api.linkedin.com/v2/organizationalEntityAcls?q=roleAssignee"
        acl_res = requests.get(acl_url, headers=headers_restli)
        if acl_res.status_code == 200:
            acl_data = acl_res.json()
            elements = acl_data.get("elements", [])
            for el in elements:
                role = el.get("role")
                if role in ["ADMINISTRATOR", "DIRECT_SPONSOR"]:
                    ent = el.get("organizationalEntity", "")
                    if ent.startswith("urn:li:organization:"):
                        org_id = ent.split(":")[-1]
                        companies.append(org_id)
        else:
            print(f"Note: Could not query organizationalEntityAcls automatically (HTTP {acl_res.status_code}).")
    except Exception as e:
        print(f"Note: Error querying organizations: {e}")

    # Fetch details for each organization found
    # Fetch details for each organization found
    options = []
    if person_urn_id:
        options.append({"name": f"Personal Feed ({name})", "urn": f"urn:li:person:{person_urn_id}"})
    else:
        options.append({"name": "Personal Feed (Unavailable - Profile scopes not authorized)", "urn": "UNAVAILABLE"})
    
    for cid in companies:
        try:
            org_url = f"https://api.linkedin.com/v2/organizations/{cid}"
            org_res = requests.get(org_url, headers=headers_restli)
            if org_res.status_code == 200:
                org_data = org_res.json()
                loc_name = org_data.get("localizedName", f"Organization {cid}")
                options.append({"name": f"Company Page: {loc_name}", "urn": f"urn:li:organization:{cid}"})
            else:
                options.append({"name": f"Company Page ID {cid}", "urn": f"urn:li:organization:{cid}"})
        except Exception:
            options.append({"name": f"Company Page ID {cid}", "urn": f"urn:li:organization:{cid}"})

    # Suggest scraped 6Frame Studio organization URN
    scraped_org_id = "112382931"
    scraped_urn = f"urn:li:organization:{scraped_org_id}"
    
    # If not already present in options list, add it
    scraped_in_options = any(opt["urn"] == scraped_urn for opt in options)
    if not scraped_in_options:
        options.append({"name": "Company Page: 6Frame Studio (Detected/Scraped)", "urn": scraped_urn})

    options.append({"name": "Custom Organization URN...", "urn": "CUSTOM"})

    print("\n" + "=" * 60)
    print("Select where the hub should publish LinkedIn posts:")
    print("=" * 60)
    for idx, opt in enumerate(options):
        print(f"[{idx + 1}] {opt['name']} ({opt['urn']})")
    
    selected_urn = None
    while True:
        try:
            sel_str = input(f"\nEnter selection [1-{len(options)}] (Default: 1): ").strip()
            if not sel_str:
                selected_urn = options[0]["urn"]
                if selected_urn == "UNAVAILABLE":
                    print("Default option is unavailable. Please select another option.")
                    continue
                break
            sel_idx = int(sel_str) - 1
            if 0 <= sel_idx < len(options):
                if options[sel_idx]["urn"] == "CUSTOM":
                    custom_urn = input("Enter custom URN (e.g. urn:li:organization:123456): ").strip()
                    if custom_urn:
                        selected_urn = custom_urn
                        break
                else:
                    selected_urn = options[sel_idx]["urn"]
                    break
            print(f"Invalid option. Please enter a number between 1 and {len(options)}.")
        except ValueError:
            print("Invalid input. Please enter a number.")

    print(f"\nSetting LinkedIn posting target to: {selected_urn}")

    # 7. Update settings.json
    settings["linkedin_access_token"] = access_token
    settings["linkedin_person_urn"] = selected_urn
    
    with open(SETTINGS_FILE, "w") as f:
        json.dump(settings, f, indent=4)

    print(f"\nSaved access token and target URN ({selected_urn}) to settings.json!")
    print("=" * 60)

if __name__ == "__main__":
    main()

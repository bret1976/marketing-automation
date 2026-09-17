import json
import requests

SETTINGS_FILE = "/Users/majid/.gemini/antigravity/scratch/marketing-automation/settings.json"

def main():
    if not json.loads(open(SETTINGS_FILE).read()).get("linkedin_access_token"):
        print("No LinkedIn access token found.")
        return

    with open(SETTINGS_FILE) as f:
        settings = json.load(f)

    access_token = settings["linkedin_access_token"]
    headers = {
        "Authorization": f"Bearer {access_token}",
        "X-Restli-Protocol-Version": "2.0.0"
    }

    # Fetch organization access control list
    print("Checking organizationalEntityAcls...")
    acl_url = "https://api.linkedin.com/v2/organizationalEntityAcls?q=roleAssignee"
    res = requests.get(acl_url, headers=headers)
    print("Status:", res.status_code)
    print("Response:", res.text)

    # Also check openid info to verify scopes
    print("\nChecking token userInfo...")
    info_res = requests.get("https://api.linkedin.com/v2/userinfo", headers=headers)
    print("UserInfo Status:", info_res.status_code)
    print("UserInfo Response:", info_res.text)

if __name__ == "__main__":
    main()

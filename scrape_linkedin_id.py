import requests
import re

def main():
    url = "https://www.linkedin.com/company/6framestudio"
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    
    res = requests.get(url, headers=headers)
    print("Status code:", res.status_code)
    
    # Search for URN or company ID patterns in the source
    content = res.text
    
    # Patterns like urn:li:organization:123456 or company/123456
    matches = re.findall(r'urn:li:organization:(\d+)', content)
    if matches:
        print("Found organization URN matches:", set(matches))
    else:
        print("No urn:li:organization matches found.")
        
    matches_comp = re.findall(r'company/(\d+)', content)
    if matches_comp:
        print("Found company/ID matches:", set(matches_comp))
        
    # Let's search for objectUrn or entityUrn
    matches_urn = re.findall(r'objectUrn.*?(\d+)', content)
    if matches_urn:
        print("Found objectUrn matches:", set(matches_urn))
        
    # Search for any 9-digit numbers or similar
    # print(content[:2000]) # Print start of page to see if we got redirected

if __name__ == "__main__":
    main()

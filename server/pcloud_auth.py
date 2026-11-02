import json, webbrowser, requests
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlencode, urlparse, parse_qs

PORT = 65432
REDIRECT_URI = f"http://localhost:{PORT}/"

with open("pcloud_credentials.json", "r", encoding="utf-8") as f:
    app = json.load(f)

result = {}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        query = parse_qs(urlparse(self.path).query)
        result["code"] = query.get("code", [None])[0]
        result["hostname"] = query.get("hostname", ["api.pcloud.com"])[0]
        result["error"] = query.get("error", [None])[0]
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"pCloud login completed. You can close this tab.")


auth_url = "https://my.pcloud.com/oauth2/authorize?" + urlencode({
    "client_id": app["client_id"],
    "response_type": "code",
    "redirect_uri": REDIRECT_URI,
})
print("If the browser does not open, visit this address:")
print(auth_url)
webbrowser.open(auth_url)

server = HTTPServer(("localhost", PORT), Handler)
server.handle_request()

if result.get("error") or not result.get("code"):
    print("Login failed:", result.get("error"))
    exit()

response = requests.get("https://" + result["hostname"] + "/oauth2_token", params={
    "client_id": app["client_id"],
    "client_secret": app["client_secret"],
    "code": result["code"],
})
data = response.json()

if data["result"] != 0:
    print("Token error:", data.get("error"))
else:
    with open("pcloud_token.json", "w", encoding="utf-8") as f:
        json.dump({"api": "https://" + result["hostname"],
                   "access_token": data["access_token"]}, f, indent=4)
    print("Token saved to pcloud_token.json")

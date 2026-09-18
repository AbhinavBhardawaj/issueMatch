import jwt
import time
import httpx

APP_ID = "4991882"
PRIVATE_KEY = open("keys/verifier-bot.pem").read()
INSTALLATION_ID = "162794652"

# Generate JWT
payload = {
    "iat": int(time.time()),
    "exp": int(time.time()) + 600,
    "iss": APP_ID
}
token = jwt.encode(payload, PRIVATE_KEY, algorithm="RS256")

# Exchange for installation token
resp = httpx.post(
    f"https://api.github.com/app/installations/{INSTALLATION_ID}/access_tokens",
    headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json"
    }
)
print(resp.status_code)
print(resp.json())
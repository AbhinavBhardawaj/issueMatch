import httpx, os
token = 'ghs_4991882_eyJhbGciOiJFUzI1NiIsInR5cCI6IkpXVCJ9.eyJhdWQiOiJhdXRobmQiLCJjdHgiOiJrZEpqaFhLcXh6QmVFZFdiYzJGbG1PTU1HTjBlWUJlMmVxTWJBTWh0WDdhR05SdTduSkJVcm8wOXFnIiwiZXhwIjoxNzg5NzUzMDk3LCJpYXQiOjE3ODk3NDk0OTcsImlzcyI6ImdpdGh1YiIsImp0aSI6IjczNWZjNDA5LTM2MDgtNGVjZS1iOTdiLTRiMzk3YzIxYWNjYSIsInZlciI6M30.kyFO1tEiDHfMtgyEpM8GxqTErEx-vJucYXH88XqVdz31NV7Tt1-0yCYUnzQSbApiFklnaU_lkWrgscPudxRbkA'
headers = {'Authorization': f'Bearer {token}', 'Accept': 'application/vnd.github+json'}
r = httpx.get('https://api.github.com/repos/koushiksuresh27/NitiFlow', headers=headers).json()
r2 = httpx.get('https://api.github.com/repos/koushiksuresh27/NitiFlow/commits/main', headers=headers).json()
print("GH_REPO_ID=" + str(r.get("id")))
print("GH_COMMIT_SHA=" + str(r2.get("sha")))

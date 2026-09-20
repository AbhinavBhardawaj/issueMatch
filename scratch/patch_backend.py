import os
import re

# 1. Update .env and .env.example
env_content = """
# GitHub OAuth App (for user login — different from the GitHub App above)
GITHUB_OAUTH_CLIENT_ID=
GITHUB_OAUTH_CLIENT_SECRET=
OAUTH_REDIRECT_URI=http://localhost:8000/api/auth/callback
FRONTEND_URL=http://localhost:5173

# Session signing key — generate with: python -c "import secrets; print(secrets.token_hex(32))"
SESSION_SECRET_KEY=cc83e6e6daffdba80279e7ee750e2d1bcf0d61395daefe848247de4d79125acd
"""

with open(".env", "a", encoding="utf-8") as f:
    f.write(env_content)

env_example_content = """
# GitHub OAuth App (for user login — different from the GitHub App above)
GITHUB_OAUTH_CLIENT_ID=
GITHUB_OAUTH_CLIENT_SECRET=
OAUTH_REDIRECT_URI=http://localhost:8000/api/auth/callback
FRONTEND_URL=http://localhost:5173

# Session signing key — generate with: python -c "import secrets; print(secrets.token_hex(32))"
SESSION_SECRET_KEY=
"""

with open(".env.example", "a", encoding="utf-8") as f:
    f.write(env_example_content)

# 2. Update app/main.py
with open("app/main.py", "r", encoding="utf-8") as f:
    content = f.read()

imports_to_add = []
if "import os" not in content:
    imports_to_add.append("import os")
if "from fastapi.middleware.cors import CORSMiddleware" not in content:
    imports_to_add.append("from fastapi.middleware.cors import CORSMiddleware")
if "from app.api.auth import router as auth_router" not in content:
    imports_to_add.append("from app.api.auth import router as auth_router")

if imports_to_add:
    content = "\\n".join(imports_to_add) + "\\n" + content

middleware_code = """
    application.add_middleware(
        CORSMiddleware,
        allow_origins=[os.getenv("FRONTEND_URL", "http://localhost:5173")],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
"""
router_code = '    application.include_router(auth_router, prefix="/api")\\n'

app_match = re.search(r'application = FastAPI\\(title="IssueMatch", lifespan=lifespan\\)', content)
if app_match:
    insertion_point = app_match.end()
    content = content[:insertion_point] + "\\n" + middleware_code + "\\n" + router_code + content[insertion_point:]

with open("app/main.py", "w", encoding="utf-8") as f:
    f.write(content)

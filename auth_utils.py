import os

try:
    import bcrypt
except ImportError:  # Password auth can fail without disabling Google OAuth.
    bcrypt = None


def _is_dev_dummy_enabled() -> bool:
    return os.environ.get("GOOGLE_OAUTH_DEV_DUMMY", "").lower() in ("1", "true", "yes")

# Password hashing
def hash_password(password: str) -> str:
    if bcrypt is None:
        raise RuntimeError("bcrypt is required for password auth")
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(password.encode('utf-8'), salt).decode('utf-8')

def verify_password(password: str, hashed: str) -> bool:
    if bcrypt is None:
        raise RuntimeError("bcrypt is required for password auth")
    if not hashed:
        return False
    return bcrypt.checkpw(password.encode('utf-8'), hashed.encode('utf-8'))

# Google OAuth placeholder (actual implementation will need client secrets)
# For now, I'll provide the logic, but the user will need to set up the Google Cloud Project.
def get_google_auth_url(redirect_uri: str):
    GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
    GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")
    if redirect_uri.startswith("http://localhost") or redirect_uri.startswith("http://127.0.0.1"):
        os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")
    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
        # Dummy dev flow — only active when GOOGLE_OAUTH_DEV_DUMMY=1 is explicitly set.
        # Never allow this in production (where GOOGLE_CLIENT_ID should always be present).
        if _is_dev_dummy_enabled():
            return f"/api/auth/google/callback?code=dummy_code&state=dummy_state", "dummy_state"
        raise RuntimeError(
            "Google OAuth is not configured. Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET, "
            "or set GOOGLE_OAUTH_DEV_DUMMY=1 to use a local dev placeholder."
        )

    from google_auth_oauthlib.flow import Flow

    client_config = {
        "web": {
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [redirect_uri]
        }
    }

    flow = Flow.from_client_config(
        client_config,
        scopes=['https://www.googleapis.com/auth/userinfo.email', 'openid', 'https://www.googleapis.com/auth/userinfo.profile']
    )
    flow.redirect_uri = redirect_uri
    authorization_url, state = flow.authorization_url(
        access_type='offline',
        include_granted_scopes='true'
    )
    return authorization_url, state

def verify_google_token(code: str, redirect_uri: str, state: str):
    GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
    GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")
    if redirect_uri.startswith("http://localhost") or redirect_uri.startswith("http://127.0.0.1"):
        os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")
    # Only accept dummy tokens when the dummy dev flow is explicitly enabled.
    if code == "dummy_code" and state == "dummy_state" and _is_dev_dummy_enabled():
        return {
            "email": "dev@example.com",
            "sub": "dummy_google_id",
            "name": "Developer"
        }

    from google_auth_oauthlib.flow import Flow
    from google.oauth2 import id_token
    from google.auth.transport import requests
    
    client_config = {
        "web": {
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [redirect_uri]
        }
    }

    flow = Flow.from_client_config(client_config, scopes=['https://www.googleapis.com/auth/userinfo.email', 'openid', 'https://www.googleapis.com/auth/userinfo.profile'], state=state)
    flow.redirect_uri = redirect_uri
    flow.fetch_token(code=code)
    
    credentials = flow.credentials
    id_info = id_token.verify_oauth2_token(
        credentials.id_token, requests.Request(), GOOGLE_CLIENT_ID
    )
    return id_info

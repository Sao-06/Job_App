import os

try:
    import bcrypt
    _bcrypt_import_error: str | None = None
except (ImportError, OSError) as _exc:
    # ImportError covers the missing-package case. OSError catches Windows
    # DLL-load failures (bcrypt 4+ ships native code; if libffi or VC++
    # runtime is mismatched the import raises OSError on Windows, not
    # ImportError). Either way we keep the rest of auth_utils importable
    # so Google OAuth still works without password support.
    bcrypt = None
    _bcrypt_import_error = f"{type(_exc).__name__}: {_exc}"


def _is_dev_dummy_enabled() -> bool:
    return os.environ.get("GOOGLE_OAUTH_DEV_DUMMY", "").lower() in ("1", "true", "yes")


def _bcrypt_unavailable_message() -> str:
    detail = f" Underlying error: {_bcrypt_import_error}." if _bcrypt_import_error else ""
    return (
        "Password sign-in requires the `bcrypt` package, which isn't installed "
        "in this Python environment. Run `pip install 'bcrypt>=4.1.0'` in the "
        "same interpreter you used to launch app.py, then restart the server."
        f"{detail}"
    )


# Password hashing
def hash_password(password: str) -> str:
    if bcrypt is None:
        raise RuntimeError(_bcrypt_unavailable_message())
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(password.encode('utf-8'), salt).decode('utf-8')

def verify_password(password: str, hashed: str) -> bool:
    if bcrypt is None:
        raise RuntimeError(_bcrypt_unavailable_message())
    if not hashed:
        return False
    return bcrypt.checkpw(password.encode('utf-8'), hashed.encode('utf-8'))

# Google OAuth placeholder (actual implementation will need client secrets)
# For now, I'll provide the logic, but the user will need to set up the Google Cloud Project.
def get_google_auth_url(redirect_uri: str):
    GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
    GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")
    # Google frequently returns a granted-scope set that differs (order or
    # extra scopes like `openid`) from what we requested. oauthlib treats that
    # as a hard error ("Scope has changed…") during fetch_token unless this is
    # set — the single most common reason a successful Google consent still
    # bounces the user back to the sign-in page. Not transport-specific, so it
    # applies in production too, not just localhost.
    os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")
    if redirect_uri.startswith("http://localhost") or redirect_uri.startswith("http://127.0.0.1"):
        os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")
    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
        # Dummy dev flow — only active when GOOGLE_OAUTH_DEV_DUMMY=1 is explicitly set.
        # Never allow this in production (where GOOGLE_CLIENT_ID should always be present).
        if _is_dev_dummy_enabled():
            return f"/api/auth/google/callback?code=dummy_code&state=dummy_state", "dummy_state", None
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
    # google-auth-oauthlib auto-enables PKCE: authorization_url() generated a
    # secret code_verifier and sent only its hash (code_challenge) to Google.
    # Google will demand the raw verifier back at token exchange. Because the
    # callback builds a *fresh* Flow in a different request, we must persist
    # this verifier alongside `state` and replay it in verify_google_token —
    # otherwise fetch_token fails with "(invalid_grant) Missing code verifier"
    # and the user silently bounces back to the sign-in page.
    code_verifier = getattr(flow, "code_verifier", None)
    return authorization_url, state, code_verifier

def verify_google_token(code: str, redirect_uri: str, state: str, code_verifier: str | None = None):
    GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
    GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")
    # See get_google_auth_url: relax oauthlib's strict scope-equality check so a
    # granted-scope superset from Google doesn't make fetch_token raise.
    os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")
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
    # Replay the PKCE verifier captured in get_google_auth_url. fetch_token does
    # `kwargs.setdefault("code_verifier", self.code_verifier)`, so setting this
    # attribute is what actually sends it to Google's token endpoint.
    if code_verifier:
        flow.code_verifier = code_verifier
    flow.fetch_token(code=code)
    
    credentials = flow.credentials
    id_info = id_token.verify_oauth2_token(
        credentials.id_token, requests.Request(), GOOGLE_CLIENT_ID
    )
    return id_info

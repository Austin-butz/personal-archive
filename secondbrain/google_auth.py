"""Google OAuth for a Desktop client: one interactive consent, then refresh only.

The client secret and the token live in ~/.config/secondbrain/, outside the repo,
readable only by you. `secondbrain auth google` does the one-time browser consent
(loopback redirect on 127.0.0.1, PKCE). Everything else calls `session()`, which
refreshes the access token as needed and never opens a browser.
"""
import json
import os
from pathlib import Path

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import AuthorizedSession, Request
from google.oauth2.credentials import Credentials

CONFIG_DIR = Path(os.environ.get("SECONDBRAIN_CONFIG", "~/.config/secondbrain")).expanduser()
CLIENT_FILE = CONFIG_DIR / "google_client_secret.json"
TOKEN_FILE = CONFIG_DIR / "google_token.json"
PENDING_FILE = CONFIG_DIR / "google_auth_pending.json"  # between `--headless` and `--finish`
HEADLESS_REDIRECT = "http://localhost:8765/"  # nothing listens; you copy the URL back

_BASE = "https://www.googleapis.com/auth/googlehealth."
NUTRITION_WRITE = _BASE + "nutrition.writeonly"  # `log` pushes meals and water
SCOPES = [_BASE + s for s in ("activity_and_fitness.readonly",
                              "health_metrics_and_measurements.readonly",
                              "sleep.readonly")] + [NUTRITION_WRITE]


def _save(creds: Credentials):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(creds.to_json())


def _check_client():
    if not CLIENT_FILE.exists():
        raise FileNotFoundError(f"put your Desktop OAuth client JSON at {CLIENT_FILE}")
    if "PASTE_" in CLIENT_FILE.read_text():
        raise ValueError(f"fill in client_id and client_secret in {CLIENT_FILE}")


def authorize() -> Credentials:
    """Interactive: opens a browser for consent and stores the refresh token."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    _check_client()
    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_FILE), SCOPES)
    # prompt=consent guarantees a refresh token even if you've consented before.
    creds = flow.run_local_server(port=0, prompt="consent", access_type="offline")
    _save(creds)
    return creds


def headless_url() -> str:
    """Over SSH, step 1: the consent URL. Its PKCE verifier waits in PENDING_FILE."""
    from google_auth_oauthlib.flow import Flow

    _check_client()
    flow = Flow.from_client_secrets_file(str(CLIENT_FILE), SCOPES, redirect_uri=HEADLESS_REDIRECT,
                                         autogenerate_code_verifier=True)
    url, state = flow.authorization_url(prompt="consent", access_type="offline")
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(PENDING_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"state": state, "code_verifier": flow.code_verifier}, f)
    return url


def headless_finish(redirected: str) -> Credentials:
    """Step 2: the localhost URL the browser landed on (which failed to load) -> token."""
    from urllib.parse import parse_qs, urlparse

    from google_auth_oauthlib.flow import Flow

    if not PENDING_FILE.exists():
        raise RuntimeError("no sign-in in progress: run `auth google --headless` first")
    pending = json.loads(PENDING_FILE.read_text())
    q = parse_qs(urlparse(redirected.strip()).query)
    if "error" in q:
        raise RuntimeError(f"Google returned an error: {q['error'][0]}")
    if q.get("state", [None])[0] != pending["state"]:
        raise RuntimeError("that URL is from a different sign-in attempt; use the latest link")
    flow = Flow.from_client_secrets_file(str(CLIENT_FILE), SCOPES, redirect_uri=HEADLESS_REDIRECT,
                                         state=pending["state"], code_verifier=pending["code_verifier"])
    flow.fetch_token(code=q["code"][0])
    _save(flow.credentials)
    PENDING_FILE.unlink()
    return flow.credentials


def session(require: str | None = None) -> AuthorizedSession:
    """Non-interactive: an HTTP session with a fresh access token, or a clear error.

    The token keeps the scopes it was granted, so a token from before a scope was added
    to SCOPES still refreshes; only callers that `require` the new scope are refused."""
    if not TOKEN_FILE.exists():
        raise RuntimeError("no Google token: run `secondbrain auth google` once")
    creds = Credentials.from_authorized_user_info(json.loads(TOKEN_FILE.read_text()))
    if require and require not in (creds.scopes or ()):
        raise RuntimeError(f"the Google token wasn't granted {require}: re-run "
                           "`secondbrain auth google` and allow it")
    if not creds.valid:
        try:
            creds.refresh(Request())
        except RefreshError as e:
            raise RuntimeError(f"Google refresh failed ({e}); if the OAuth app is in "
                               "'Testing' its tokens expire after 7 days. Re-run "
                               "`secondbrain auth google`.") from e
        _save(creds)
    return AuthorizedSession(creds)

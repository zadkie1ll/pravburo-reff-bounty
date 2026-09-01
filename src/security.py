import hmac
import secrets


def csrf_token(session: dict) -> str:
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


def valid_csrf(session: dict, submitted: str) -> bool:
    expected = session.get("csrf_token", "")
    return bool(expected and submitted and hmac.compare_digest(expected, submitted))

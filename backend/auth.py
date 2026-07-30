import os
import re
import time
import logging
import bcrypt
import jwt
import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

# NOTE: the DynamoDB table's partition key attribute is named "username" (from
# when accounts were username-based) but every account is now identified by
# email -- we just store the email string in that same key attribute rather
# than recreating the table.
USERS_TABLE_NAME = os.getenv("DYNAMODB_USERS_TABLE_NAME", "BuildCareUsers")
AWS_REGION = os.getenv("AWS_REGION", "ap-southeast-1")
JWT_SECRET = os.getenv("JWT_SECRET")
JWT_ALGORITHM = "HS256"
TOKEN_TTL_SECONDS = 7 * 24 * 3600  # 7 days

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
VALID_ROLES = ("user", "admin")
VALID_TOURS = ("user", "admin")

_table_cache = None


def _get_table(force_reconnect=False):
    global _table_cache
    if _table_cache is not None and not force_reconnect:
        return _table_cache
    dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
    _table_cache = dynamodb.Table(USERS_TABLE_NAME)
    return _table_cache


def init_users_table():
    try:
        _get_table().load()
        logger.info(f"DynamoDB table '{USERS_TABLE_NAME}' initialised successfully.")
    except Exception as e:
        logger.error(f"Failed to initialise Users table: {e}")


class AuthError(Exception):
    """Raised for any signup/login failure with a user-facing message."""


def check_password_strength(password: str) -> None:
    """Raises AuthError with a specific message if the password doesn't meet policy:
    8+ chars, at least one uppercase, one lowercase, one digit, one special char."""
    if not password or len(password) < 8:
        raise AuthError("Password must be at least 8 characters.")
    if not re.search(r"[A-Z]", password):
        raise AuthError("Password must include an uppercase letter.")
    if not re.search(r"[a-z]", password):
        raise AuthError("Password must include a lowercase letter.")
    if not re.search(r"[0-9]", password):
        raise AuthError("Password must include a number.")
    if not re.search(r"[^A-Za-z0-9]", password):
        raise AuthError("Password must include a special character.")


def _make_token(email: str, role: str) -> dict:
    payload = {"sub": email, "role": role, "exp": int(time.time()) + TOKEN_TTL_SECONDS}
    token = jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)
    return {"token": token, "email": email, "role": role}


def signup(email: str, password: str, role: str) -> dict:
    """Creates the account and logs it in immediately -- no email verification,
    no admin approval queue. Self-declaring 'admin' here does grant admin access
    right away, so treat this endpoint as trusted-network-only if that's not desired."""
    email = (email or "").strip().lower()
    role = (role or "user").strip().lower()
    if not EMAIL_RE.match(email):
        raise AuthError("Please enter a valid email address.")
    check_password_strength(password)
    if role not in VALID_ROLES:
        raise AuthError("Role must be 'user' or 'admin'.")

    table = _get_table()
    password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    try:
        table.put_item(
            Item={
                "username": email,  # DynamoDB key attribute -- holds the email, see note above
                "password_hash": password_hash,
                "role": role,
                "created_at": int(time.time()),
            },
            ConditionExpression="attribute_not_exists(username)",
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise AuthError("An account with this email already exists.")
        raise
    return {"email": email, "role": role}


def login(email: str, password: str) -> dict:
    email = (email or "").strip().lower()
    table = _get_table()
    item = table.get_item(Key={"username": email}).get("Item")
    if not item or not bcrypt.checkpw((password or "").encode(), item["password_hash"].encode()):
        raise AuthError("Invalid email or password.")
    return _make_token(email, item["role"])


def verify_token(token: str) -> dict:
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.PyJWTError:
        raise AuthError("Invalid or expired token.")
    return {"email": payload["sub"], "role": payload["role"]}


def get_tour_status(email: str) -> dict:
    """Whether this account has already dismissed each tour -- stored on the
    account itself (not localStorage) so it's consistent across devices/browsers."""
    table = _get_table()
    item = table.get_item(Key={"username": email}).get("Item") or {}
    return {
        "seen_tour_user": bool(item.get("seen_tour_user", False)),
        "seen_tour_admin": bool(item.get("seen_tour_admin", False)),
    }


def mark_tour_seen(email: str, tour: str) -> dict:
    if tour not in VALID_TOURS:
        raise AuthError("Invalid tour name.")
    field = f"seen_tour_{tour}"
    table = _get_table()
    table.update_item(
        Key={"username": email},
        UpdateExpression="SET #f = :true",
        ExpressionAttributeNames={"#f": field},
        ExpressionAttributeValues={":true": True},
    )
    return {"tour": tour, "seen": True}

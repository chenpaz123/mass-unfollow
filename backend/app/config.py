import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
AVATAR_DIR = DATA_DIR / "avatars"
DB_PATH = DATA_DIR / "app.db"
IG_SESSION_PATH = DATA_DIR / "ig_session.json"
IG_DEVICE_PATH = DATA_DIR / "ig_device.json"
VAPID_PRIVATE_KEY_PATH = DATA_DIR / "vapid_private_key.pem"

APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
SECRET_KEY = os.environ.get("SECRET_KEY", "")

DEFAULT_DAILY_CAP = int(os.environ.get("DEFAULT_DAILY_CAP", "150"))
DEFAULT_MIN_DELAY_SEC = int(os.environ.get("DEFAULT_MIN_DELAY_SEC", "45"))
DEFAULT_MAX_DELAY_SEC = int(os.environ.get("DEFAULT_MAX_DELAY_SEC", "180"))

# Delay range instagrapi waits between its own internal API calls.
IG_DELAY_RANGE = [
    int(os.environ.get("IG_DELAY_MIN", "2")),
    int(os.environ.get("IG_DELAY_MAX", "5")),
]

DATA_DIR.mkdir(parents=True, exist_ok=True)
AVATAR_DIR.mkdir(parents=True, exist_ok=True)

# Stamped into the image at build time (see Dockerfile) — changes exactly
# when the app code actually changes, used to detect stale clients.
VERSION_FILE = Path("/srv/VERSION")
try:
    APP_VERSION = VERSION_FILE.read_text().strip()
except OSError:
    APP_VERSION = "dev"


def _load_or_create_vapid_keypair() -> str:
    """Web Push key pair for browser notifications (e.g. the unfollow worker
    auto-pausing). Generated once and persisted -- a browser's push
    subscription is permanently tied to the public key it subscribed with,
    so rotating this later would silently break every existing subscription
    (they'd all need to re-enable notifications). Returns the public key as
    the base64url-encoded uncompressed EC point the browser Push API expects
    for applicationServerKey; the private key is used straight from
    VAPID_PRIVATE_KEY_PATH by pywebpush, never held in memory as a string.
    """
    import base64

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    if VAPID_PRIVATE_KEY_PATH.exists():
        private_key = serialization.load_pem_private_key(VAPID_PRIVATE_KEY_PATH.read_bytes(), password=None)
    else:
        private_key = ec.generate_private_key(ec.SECP256R1())
        pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        VAPID_PRIVATE_KEY_PATH.write_bytes(pem)

    numbers = private_key.public_key().public_numbers()
    uncompressed_point = b"\x04" + numbers.x.to_bytes(32, "big") + numbers.y.to_bytes(32, "big")
    return base64.urlsafe_b64encode(uncompressed_point).rstrip(b"=").decode()


VAPID_PUBLIC_KEY = _load_or_create_vapid_keypair()

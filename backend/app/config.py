import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
AVATAR_DIR = DATA_DIR / "avatars"
DB_PATH = DATA_DIR / "app.db"
IG_SESSION_PATH = DATA_DIR / "ig_session.json"

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

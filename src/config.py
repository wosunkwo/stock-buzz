"""Project-wide configuration constants."""
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Load environment variables from .env if present. Does nothing if the file
# is missing — shell exports still work as a fallback.
load_dotenv(PROJECT_ROOT / ".env")
DATA_DIR = PROJECT_ROOT / "data"
OUTPUT_DIR = PROJECT_ROOT / "output"
DB_PATH = DATA_DIR / "buzz.db"
REPORT_PATH = OUTPUT_DIR / "report.html"

# Rolling window for what counts as "buzzy".
BUZZ_WINDOW_HOURS = 24

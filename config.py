import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
MAX_COMMENTS_PER_ACCOUNT = int(os.getenv("MAX_COMMENTS_PER_ACCOUNT", "50"))
DELAY_MIN = int(os.getenv("DELAY_BETWEEN_COMMENTS_MIN", "30"))
DELAY_MAX = int(os.getenv("DELAY_BETWEEN_COMMENTS_MAX", "90"))
WEBAPP_URL = os.getenv("WEBAPP_URL", "")
DB_PATH = os.path.join(os.path.dirname(__file__), "data.db")

import os
import sys
import time
import random
import logging
import argparse
import re
import sqlite3
import threading
from typing import List, Dict, Any, Optional, Tuple, Union
from urllib.parse import urljoin

# Try curl_cffi for bypassing Cloudflare / Akamai TLS fingerprint checks
try:
    from curl_cffi import requests as curl_requests
    HAS_CURL_CFFI = True
except ImportError:
    HAS_CURL_CFFI = False

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

try:
    from bs4 import BeautifulSoup
    HAS_BS4 = True
except ImportError:
    HAS_BS4 = False

# ==========================================
# CONFIGURATION
# ==========================================

# SQLite database path - configurable via environment variable.
# Default: /app/data/app.db (Docker persistent volume mount path).
# Override with: DATABASE_URL=sqlite:///path/to/app.db
# or:            DB_PATH=/custom/path/app.db
def _resolve_db_path() -> str:
    """Resolves the SQLite database file path from environment, with safe fallback."""
    # Check DATABASE_URL first (sqlite:///path style)
    db_url = os.getenv("DATABASE_URL", "").strip()
    if db_url:
        if db_url.startswith("sqlite:///"):
            path = db_url[len("sqlite:///"):]
            if path:
                return path
        elif db_url.startswith("sqlite://"):
            path = db_url[len("sqlite://"):]
            if path:
                return path

    # Check DB_PATH directly
    db_path = os.getenv("DB_PATH", "").strip()
    if db_path:
        return db_path

    # Default: /app/data/app.db inside container (volume-mounted), or local fallback
    default = "/app/data/app.db"
    # If /app/data doesn't exist and we're not in Docker, fall back to local directory
    if not os.path.isdir("/app/data") and not os.path.isdir("/app"):
        default = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "app.db")
    return default

DB_PATH = _resolve_db_path()

BASE_URL = "https://www.investing.com"
MAX_PAGES_PER_USER = int(os.getenv('MAX_PAGES_PER_USER', '1'))
REQUEST_TIMEOUT = int(os.getenv('REQUEST_TIMEOUT', '20'))
REQUEST_DELAY_MIN = float(os.getenv('REQUEST_DELAY_MIN', '1.5'))
REQUEST_DELAY_MAX = float(os.getenv('REQUEST_DELAY_MAX', '3.0'))

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": "https://www.investing.com/",
    "Sec-Ch-Ua": '"Chromium";v="128", "Not;A=Brand";v="24", "Google Chrome";v="128"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1"
}

_log_handlers = [logging.StreamHandler(sys.stdout)]
_log_file_path = os.getenv('LOG_FILE', 'comment_scraper.log')
if os.path.isdir(_log_file_path):
    _log_file_path = os.path.join(_log_file_path, 'comment_scraper.log')

try:
    _log_dir = os.path.dirname(os.path.abspath(_log_file_path))
    if _log_dir and not os.path.exists(_log_dir):
        os.makedirs(_log_dir, exist_ok=True)
    _log_handlers.append(logging.FileHandler(_log_file_path, encoding='utf-8'))
except Exception:
    pass

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=_log_handlers
)
logger = logging.getLogger("InvestingScraper")


# ==========================================
# HTML PARSER LOGIC
# ==========================================

def normalize_comment_url_and_id(raw_href: str, base_url: str = BASE_URL) -> Tuple[Optional[int], Optional[str]]:
    """
    Normalizes comment URLs and extracts the target comment ID.

    Handles:
    1. Direct comments:
       '/commodities/silver-commentary?comment=47571123'
       -> ID: 47571123, URL: 'https://www.investing.com/commodities/silver-commentary?comment=47571123'
    2. Reply/Subcomments with fragment:
       '/commodities/gold-commentary?comment=47571018#47571035'
       -> ID: 47571035 (from after #), URL: 'https://www.investing.com/commodities/gold-commentary?comment=47571035'
    """
    if not raw_href or 'comment=' not in raw_href:
        return None, None

    # Check for reply anchor after '#' (e.g., #47571035)
    hash_match = re.search(r'#(\d+)', raw_href)
    param_match = re.search(r'comment=(\d+)', raw_href)

    if hash_match:
        comment_id = int(hash_match.group(1))
    elif param_match:
        comment_id = int(param_match.group(1))
    else:
        return None, None

    # Strip query string and fragment to get the base path (e.g. /commodities/gold-commentary)
    base_path = raw_href.split('?')[0].split('#')[0].strip()
    if not base_path:
        return None, None

    clean_href = f"{base_path}?comment={comment_id}"
    full_url = urljoin(base_url, clean_href)

    return comment_id, full_url


def extract_comments_from_html(
    html_content: str,
    base_url: str = BASE_URL,
    user_id: int = 0,
    user_name: str = ""
) -> List[Dict[str, Any]]:
    if not html_content:
        return []

    if HAS_BS4:
        return _extract_with_bs4(html_content, base_url, user_id, user_name)
    else:
        return _extract_with_regex(html_content, base_url, user_id, user_name)


def _extract_with_bs4(html_content: str, base_url: str, user_id: int, user_name: str = "") -> List[Dict[str, Any]]:
    soup = BeautifulSoup(html_content, 'html.parser')
    comments = []
    seen_ids = set()

    comment_divs = soup.find_all('div', class_=lambda c: c and 'comment' in c.split())

    for div in comment_divs:
        body = div.find('div', class_='commentBody') or div
        link_tag = body.find('a', href=True)
        if not link_tag:
            continue

        raw_href = link_tag.get('href', '').strip()
        comment_id, full_url = normalize_comment_url_and_id(raw_href, base_url)
        if not comment_id or not full_url:
            continue

        # Extract exact comment text corresponding to this comment/reply ID
        comment_text = ""
        text_div = div.find('div', id=f'commentText-{comment_id}') or body.find('div', id=f'commentText-{comment_id}')
        if not text_div:
            text_div = body.find('div', id=lambda x: x and x.startswith('commentText-')) or div.find('div', id=lambda x: x and x.startswith('commentText-'))

        if text_div:
            comment_text = text_div.get_text(separator=' ', strip=True)

        if comment_id in seen_ids:
            continue

        seen_ids.add(comment_id)
        comments.append({
            'id': comment_id,
            'url': full_url,
            'user_id': user_id,
            'user_name': user_name,
            'comment_text': comment_text
        })

    return comments


def _extract_with_regex(html_content: str, base_url: str, user_id: int, user_name: str = "") -> List[Dict[str, Any]]:
    comments = []
    seen_ids = set()

    pattern = re.compile(
        r'<div[^>]*class=["\'][^"\']*comment userComment[^"\']*["\'][^>]*>(.*?)</div>\s*</div>\s*</div>',
        re.DOTALL | re.IGNORECASE
    )

    blocks = pattern.findall(html_content)
    if not blocks:
        link_text_pattern = re.compile(
            r'<a[^>]*href=["\']([^"\']+)["\'][^>]*>.*?</a>.*?<div[^>]*id=["\']commentText-(\d+)["\'][^>]*>(.*?)</div>',
            re.DOTALL | re.IGNORECASE
        )
        for href, c_id, txt in link_text_pattern.findall(html_content):
            raw_href = href.strip()
            comment_id, full_url = normalize_comment_url_and_id(raw_href, base_url)
            if not comment_id or not full_url:
                continue

            if comment_id in seen_ids:
                continue
            seen_ids.add(comment_id)
            comments.append({
                'id': comment_id,
                'url': full_url,
                'user_id': user_id,
                'user_name': user_name,
                'comment_text': re.sub(r'<[^>]+>', '', txt).strip()
            })
        return comments

    for block in blocks:
        href_match = re.search(r'<a[^>]*href=["\']([^"\']+)["\']', block)
        if not href_match:
            continue
        raw_href = href_match.group(1).strip()

        comment_id, full_url = normalize_comment_url_and_id(raw_href, base_url)
        if not comment_id or not full_url:
            continue

        if comment_id in seen_ids:
            continue

        # Extract text from commentText div matching id or any commentText div in block
        text_match = re.search(rf'<div[^>]*id=["\']commentText-{comment_id}["\'][^>]*>(.*?)</div>', block, re.DOTALL)
        if not text_match:
            text_match = re.search(r'<div[^>]*id=["\']commentText-\d+["\'][^>]*>(.*?)</div>', block, re.DOTALL)

        comment_text = re.sub(r'<[^>]+>', '', text_match.group(1)).strip() if text_match else ""

        seen_ids.add(comment_id)
        comments.append({
            'id': comment_id,
            'url': full_url,
            'user_id': user_id,
            'user_name': user_name,
            'comment_text': comment_text
        })

    return comments

# ==========================================
# DATABASE MANAGER (SQLite)
# ==========================================

class DatabaseManager:
    """
    SQLite-backed database manager.

    Replaces the previous SQL Server implementation (pyodbc / pymssql).
    Preserves exactly the same public interface and behavior:
      - get_connection()
      - get_users(limit)
      - get_unprocessed_users(limit)
      - get_user_name(user_id)
      - get_user_by_name(user_name)
      - insert_comment_urls(comments)
      - close()

    Tables created / managed:
      users        – mirrors InvestingDB.dbo.users (id INTEGER PK, user_name TEXT)
      comment_urls – mirrors InvestingDB.dbo.comment_urls
                     (id INTEGER PK, url TEXT, user_id INTEGER, status TEXT,
                      fetched_at TEXT, Comments TEXT, user_name TEXT)

    The database file path is read from DB_PATH or DATABASE_URL env vars.
    Default: /app/data/app.db (Docker persistent volume).
    """

    # Class-level lock: SQLite allows concurrent reads but serialises writes.
    _write_lock = threading.Lock()

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self.conn: Optional[sqlite3.Connection] = None
        self._schema_initialised = False
        self._user_name_col_cached: Optional[str] = None
        self._ensure_data_dir()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ensure_data_dir(self):
        """Creates the directory that will hold the .db file if it doesn't exist."""
        data_dir = os.path.dirname(self.db_path)
        if data_dir and not os.path.isdir(data_dir):
            try:
                os.makedirs(data_dir, exist_ok=True)
                logger.info(f"Created SQLite data directory: {data_dir}")
            except OSError as exc:
                logger.warning(f"Could not create data directory '{data_dir}': {exc}")

    def get_connection(self) -> sqlite3.Connection:
        """
        Returns a live SQLite connection, (re-)connecting if needed.
        Enables WAL mode for better concurrent read performance.
        """
        if self.conn:
            try:
                self.conn.execute("SELECT 1")
                return self.conn
            except Exception:
                self.close()

        logger.info(f"Connecting to SQLite database: {self.db_path}")
        # check_same_thread=False: connection is shared across threads;
        # callers must serialise writes via _write_lock.
        self.conn = sqlite3.connect(
            self.db_path,
            timeout=30.0,
            check_same_thread=False,
            isolation_level=None   # autocommit; we manage transactions explicitly
        )
        self.conn.execute("PRAGMA busy_timeout = 30000;")
        self.conn.execute("PRAGMA synchronous = NORMAL;")
        # DELETE / TRUNCATE mode avoids WAL shared-memory (.shm) locking issues on Windows/Docker mounted filesystems
        try:
            self.conn.execute("PRAGMA journal_mode = DELETE;")
        except Exception:
            pass
        # Use Row factory so rows support index access like tuples
        self.conn.row_factory = sqlite3.Row
        self._initialise_schema()
        return self.conn

    def _initialise_schema(self):
        """
        Creates tables if they do not already exist and adds any missing columns.
        Schema mirrors the SQL Server original:
          dbo.users        -> users
          dbo.comment_urls -> comment_urls
        """
        if self._schema_initialised:
            return

        conn = self.conn
        with self._write_lock:
            conn.execute("BEGIN")
            try:
                # --- users table ---
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS users (
                        id        INTEGER PRIMARY KEY,
                        user_name TEXT
                    )
                """)

                # --- comment_urls table ---
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS comment_urls (
                        id         INTEGER PRIMARY KEY,
                        url        TEXT    NOT NULL,
                        user_id    INTEGER,
                        status     TEXT    DEFAULT 'not processed',
                        fetched_at TEXT,
                        Comments   TEXT,
                        user_name  TEXT
                    )
                """)

                # Ensure Comments and user_name columns exist (migration safety for existing DBs)
                existing_cols = {
                    row[1].lower()
                    for row in conn.execute("PRAGMA table_info(comment_urls)")
                }
                if 'comments' not in existing_cols:
                    conn.execute("ALTER TABLE comment_urls ADD COLUMN Comments TEXT")
                if 'user_name' not in existing_cols:
                    conn.execute("ALTER TABLE comment_urls ADD COLUMN user_name TEXT")

                # Index for fast UPSERT existence checks on (user_id, url)
                conn.execute("""
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_comment_urls_user_url
                    ON comment_urls (user_id, url)
                """)

                # --- frontend_data table (for UI & external state storage) ---
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS frontend_data (
                        id         TEXT PRIMARY KEY,
                        user_id    INTEGER,
                        user_name  TEXT,
                        data       TEXT,
                        status     TEXT DEFAULT 'active',
                        created_at TEXT,
                        updated_at TEXT
                    )
                """)

                conn.execute("COMMIT")
                self._schema_initialised = True
                logger.info("SQLite schema initialised successfully.")
            except Exception as exc:
                conn.execute("ROLLBACK")
                logger.error(f"Failed to initialise SQLite schema: {exc}")
                raise

    # ------------------------------------------------------------------
    # User-name column detection (mirrors original dynamic detection)
    # ------------------------------------------------------------------

    def _get_user_name_column(self) -> Optional[str]:
        """
        Dynamically identifies the column name used for user name in the users table.
        Mirrors the original SQL Server INFORMATION_SCHEMA.COLUMNS logic via PRAGMA table_info.
        """
        if self._user_name_col_cached is not None:
            return self._user_name_col_cached

        try:
            conn = self.get_connection()
            # PRAGMA table_info returns: (cid, name, type, notnull, dflt_value, pk)
            cols = [row[1].lower() for row in conn.execute("PRAGMA table_info(users)")]
            for candidate in ['user_name', 'username', 'name', 'profile_name',
                              'profilename', 'display_name', 'user']:
                if candidate in cols:
                    self._user_name_col_cached = candidate
                    return self._user_name_col_cached
        except Exception as exc:
            logger.debug(f"Could not determine user name column in users table: {exc}")

        self._user_name_col_cached = None
        return None

    # ------------------------------------------------------------------
    # Public API – identical signatures to original DatabaseManager
    # ------------------------------------------------------------------

    def get_user_name(self, user_id: int) -> str:
        """Fetches the user name for a given user ID from the users table."""
        name_col = self._get_user_name_column() or 'user_name'
        try:
            conn = self.get_connection()
            row = conn.execute(
                f"SELECT {name_col} FROM users WHERE id = ?", (user_id,)
            ).fetchone()
            if row and row[0] is not None:
                return str(row[0]).strip()
        except Exception as exc:
            logger.debug(f"Could not fetch user name for user_id={user_id}: {exc}")
        return ""

    def get_user_by_name(self, user_name: str) -> Optional[Dict[str, Any]]:
        """Fetches user (id and name) for a given user name from the users table."""
        if not user_name or not str(user_name).strip():
            return None
        cleaned_name = str(user_name).strip()
        name_col = self._get_user_name_column() or 'user_name'
        try:
            conn = self.get_connection()
            # SQLite equivalent of: WHERE LOWER(LTRIM(RTRIM(col))) = LOWER(?)
            row = conn.execute(
                f"SELECT id, {name_col} FROM users WHERE LOWER(TRIM({name_col})) = LOWER(?)",
                (cleaned_name,)
            ).fetchone()
            if row and row[0] is not None:
                u_id = int(row[0])
                u_name = str(row[1]).strip() if (len(row) > 1 and row[1] is not None) else cleaned_name
                return {'id': u_id, 'user_name': u_name}
        except Exception as exc:
            logger.debug(f"Could not fetch user by name '{cleaned_name}': {exc}")
        return None

    def get_users(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """
        Fetches users from the users table status-independently (without filtering by status).
        Returns a list of dicts: [{'id': <int>, 'user_name': <str>}, ...]
        Mirrors original SQL Server behavior; uses LIMIT instead of TOP N.
        """
        conn = self.get_connection()
        name_col = self._get_user_name_column()
        select_cols = f"id, {name_col}" if name_col else "id"
        # SQLite uses LIMIT instead of TOP N
        limit_clause = f" LIMIT {int(limit)}" if limit else ""
        query = f"SELECT {select_cols} FROM users{limit_clause}"

        rows = conn.execute(query).fetchall()
        users = []
        for row in rows:
            u_id = int(row[0])
            u_name = str(row[1]).strip() if (name_col and len(row) > 1 and row[1] is not None) else ""
            users.append({'id': u_id, 'user_name': u_name})
        return users

    def get_unprocessed_users(self, limit: Optional[int] = None) -> List[int]:
        """Backward compatibility helper that returns list of user IDs."""
        return [u['id'] for u in self.get_users(limit=limit)]

    def upsert_user(self, user_id: int, user_name: str) -> Dict[str, Any]:
        """
        Inserts or updates a user in the users table.
        """
        conn = self.get_connection()
        name_col = self._get_user_name_column() or 'user_name'
        cleaned_name = str(user_name or "").strip()
        uid = int(user_id)

        with self._write_lock:
            conn.execute("BEGIN")
            try:
                conn.execute(
                    f"""
                    INSERT INTO users (id, {name_col})
                    VALUES (?, ?)
                    ON CONFLICT(id) DO UPDATE SET {name_col} = excluded.{name_col}
                    """,
                    (uid, cleaned_name)
                )
                conn.execute("COMMIT")
            except Exception as exc:
                conn.execute("ROLLBACK")
                logger.error(f"Error upserting user id={uid}: {exc}")
                raise

        return {"id": uid, "user_name": cleaned_name}

    def delete_user(self, user_id: int) -> bool:
        """Deletes a user from the users table by ID."""
        conn = self.get_connection()
        uid = int(user_id)
        with self._write_lock:
            conn.execute("BEGIN")
            try:
                cursor = conn.execute("DELETE FROM users WHERE id = ?", (uid,))
                conn.execute("COMMIT")
                return cursor.rowcount > 0
            except Exception as exc:
                conn.execute("ROLLBACK")
                logger.error(f"Error deleting user id={uid}: {exc}")
                raise

    def insert_comment_urls(self, comments: List[Dict[str, Any]]) -> int:
        """
        Inserts comment URLs into comment_urls table with UPSERT semantics.

        Preserves original behavior:
        - Skips rows where (user_id, url) already exists (no duplicate on unique pair).
        - If a comment id already exists, skips (INSERT OR IGNORE).
        - Returns count of newly inserted rows.

        SQLite equivalent of the SQL Server IF NOT EXISTS / INSERT pattern.
        """
        if not comments:
            return 0

        conn = self.get_connection()
        inserted_count = 0

        with self._write_lock:
            conn.execute("BEGIN")
            try:
                for c in comments:
                    comment_text = c.get('comment_text', '')
                    user_name = c.get('user_name', '')
                    comment_id = c.get('id')
                    url = c.get('url', '')
                    user_id = c.get('user_id')
                    fetched_at = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())

                    if comment_id is not None:
                        # INSERT OR IGNORE respects: PK (id) and UNIQUE index (user_id, url)
                        cursor = conn.execute(
                            """
                            INSERT OR IGNORE INTO comment_urls
                                (id, url, user_id, status, fetched_at, Comments, user_name)
                            VALUES (?, ?, ?, 'not processed', ?, ?, ?)
                            """,
                            (comment_id, url, user_id, fetched_at, comment_text, user_name)
                        )
                    else:
                        # No id provided – let SQLite auto-assign ROWID
                        cursor = conn.execute(
                            """
                            INSERT OR IGNORE INTO comment_urls
                                (url, user_id, status, fetched_at, Comments, user_name)
                            VALUES (?, ?, 'not processed', ?, ?, ?)
                            """,
                            (url, user_id, fetched_at, comment_text, user_name)
                        )

                    if cursor.rowcount > 0:
                        inserted_count += 1

                conn.execute("COMMIT")
            except Exception as exc:
                conn.execute("ROLLBACK")
                logger.warning(f"Error during batch insert of comment URLs: {exc}")
                raise

        return inserted_count

    def upsert_frontend_data(self, item: Dict[str, Any]) -> Dict[str, Any]:
        """
        Inserts or updates a single record in frontend_data (SQLite).
        Supports flexible JSON payloads, status tracking, and timestamps.
        """
        import json
        import uuid
        conn = self.get_connection()
        now_str = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())

        item_id = str(item.get("id") or item.get("_id") or uuid.uuid4().hex)
        user_id = item.get("user_id") or item.get("userId")
        user_name = item.get("user_name") or item.get("username")
        status_val = str(item.get("status") or "active")

        # Normalize data payload
        payload_data = item.get("data") if "data" in item else item
        if isinstance(payload_data, (dict, list)):
            data_str = json.dumps(payload_data)
        elif payload_data is not None:
            data_str = str(payload_data)
        else:
            data_str = "{}"

        with self._write_lock:
            conn.execute("BEGIN")
            try:
                conn.execute(
                    """
                    INSERT INTO frontend_data (id, user_id, user_name, data, status, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        user_id = coalesce(excluded.user_id, frontend_data.user_id),
                        user_name = coalesce(excluded.user_name, frontend_data.user_name),
                        data = excluded.data,
                        status = excluded.status,
                        updated_at = excluded.updated_at
                    """,
                    (item_id, user_id, user_name, data_str, status_val, now_str, now_str)
                )
                conn.execute("COMMIT")
            except Exception as exc:
                conn.execute("ROLLBACK")
                logger.error(f"Error upserting frontend_data: {exc}")
                raise

        return {
            "id": item_id,
            "user_id": user_id,
            "user_name": user_name,
            "data": payload_data,
            "status": status_val,
            "updated_at": now_str
        }

    def get_frontend_data(
        self,
        limit: int = 50,
        offset: int = 0,
        item_id: Optional[str] = None,
        user_id: Optional[int] = None,
        status: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Reads records from frontend_data table with pagination and filtering."""
        import json
        conn = self.get_connection()
        query = "SELECT id, user_id, user_name, data, status, created_at, updated_at FROM frontend_data WHERE 1=1"
        params: List[Any] = []

        if item_id is not None and str(item_id).strip():
            query += " AND id = ?"
            params.append(str(item_id).strip())
        if user_id is not None:
            query += " AND user_id = ?"
            params.append(int(user_id))
        if status is not None and str(status).strip():
            query += " AND status = ?"
            params.append(str(status).strip())

        query += " ORDER BY updated_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        cursor = conn.execute(query, tuple(params))
        results = []
        for row in cursor.fetchall():
            row_dict = dict(row)
            if row_dict.get("data"):
                try:
                    row_dict["data"] = json.loads(row_dict["data"])
                except Exception:
                    pass
            results.append(row_dict)
        return results

    def delete_frontend_data(self, item_id: str) -> bool:
        """Deletes a record from frontend_data by ID."""
        conn = self.get_connection()
        with self._write_lock:
            conn.execute("BEGIN")
            try:
                cursor = conn.execute("DELETE FROM frontend_data WHERE id = ?", (str(item_id),))
                conn.execute("COMMIT")
                return cursor.rowcount > 0
            except Exception as exc:
                conn.execute("ROLLBACK")
                logger.error(f"Error deleting frontend_data id={item_id}: {exc}")
                raise

    def execute_query(
        self,
        query: str,
        params: Optional[Union[List[Any], Dict[str, Any], Tuple[Any, ...]]] = None
    ) -> Dict[str, Any]:
        """
        Executes an arbitrary SQL query directly on the SQLite database.
        Returns:
            - For SELECT / PRAGMA / EXPLAIN queries: list of rows as dicts, columns, and row_count.
            - For INSERT / UPDATE / DELETE / DDL queries: rows_affected, last_insert_rowid.
        """
        if not query or not query.strip():
            raise ValueError("Query cannot be empty.")

        cleaned_query = query.strip()
        conn = self.get_connection()
        query_type = cleaned_query.split()[0].upper() if cleaned_query.split() else ""
        is_read = query_type in ("SELECT", "PRAGMA", "EXPLAIN", "WITH")

        # Format params if passed
        sql_params: Any = ()
        if params is not None:
            if isinstance(params, list):
                sql_params = tuple(params)
            else:
                sql_params = params

        if is_read:
            cursor = conn.execute(cleaned_query, sql_params)
            columns = [col[0] for col in cursor.description] if cursor.description else []
            rows = [dict(row) for row in cursor.fetchall()]
            return {
                "type": "read",
                "columns": columns,
                "row_count": len(rows),
                "data": rows
            }
        else:
            with self._write_lock:
                conn.execute("BEGIN")
                try:
                    cursor = conn.execute(cleaned_query, sql_params)
                    rows_affected = cursor.rowcount
                    last_id = cursor.lastrowid
                    conn.execute("COMMIT")
                    return {
                        "type": "write",
                        "rows_affected": rows_affected,
                        "last_insert_rowid": last_id,
                        "message": f"Query executed successfully ({rows_affected} rows affected)."
                    }
                except Exception as exc:
                    conn.execute("ROLLBACK")
                    logger.error(f"Error executing raw SQL query: {exc}")
                    raise

    def close(self):
        """Closes the database connection."""
        if self.conn:
            try:
                self.conn.close()
            except Exception:
                pass
            self.conn = None

# ==========================================
# PROXY CONFIGURATION & UTILITIES
# ==========================================

def parse_proxy(raw_proxy: str) -> Optional[str]:
    """
    Parses a proxy string into standard URL format (http://[user:pass@]host:port).
    
    Supported input formats:
    - username:password.ip:port (e.g., user:pass.218.218.252:5432)
    - username:password@ip:port (e.g., user:pass@218.218.252:5432)
    - username:password:ip:port (e.g., user:pass:218.218.252:5432)
    - ip:port:username:password (e.g., 218.218.252:5432:user:pass)
    - ip:port (e.g., 218.218.252:5432)
    - http://..., https://..., socks5://...
    """
    if not raw_proxy:
        return None
    raw = str(raw_proxy).strip()
    if not raw or raw.startswith('#'):
        return None

    scheme = "http"
    if "://" in raw:
        scheme, raw = raw.split("://", 1)

    # 1. Format: user:pass@host:port
    if "@" in raw:
        auth, host_port = raw.rsplit("@", 1)
        return f"{scheme}://{auth}@{host_port}"

    # 2. Format: username:password.ip:port (e.g. username:password.218.218.252:5432 or user:pass.host.com:8080)
    dot_match = re.match(r"^(.+?)\.([a-zA-Z0-9\-_.]+\.[a-zA-Z0-9]+|\d{1,3}(?:\.\d{1,3})+):(\d+)$", raw)
    if dot_match:
        auth_part, host, port = dot_match.groups()
        if ":" in auth_part:
            return f"{scheme}://{auth_part}@{host}:{port}"

    # 3. Format: 4 colon-separated parts (user:pass:host:port OR host:port:user:pass)
    parts = raw.split(":")
    if len(parts) == 4:
        if parts[1].isdigit() and not parts[3].isdigit():
            # host:port:user:pass
            host, port, user, password = parts
            return f"{scheme}://{user}:{password}@{host}:{port}"
        elif parts[3].isdigit():
            # user:pass:host:port
            user, password, host, port = parts
            return f"{scheme}://{user}:{password}@{host}:{port}"

    # 4. Format: 2 parts host:port
    if len(parts) == 2 and parts[1].isdigit():
        host, port = parts
        return f"{scheme}://{host}:{port}"

    # Fallback if raw contains colon
    if ":" in raw:
        return f"{scheme}://{raw}"

    return None


def mask_proxy_url(proxy_url: Optional[str]) -> str:
    """Masks proxy credentials for safe logging and output."""
    if not proxy_url:
        return "None"
    return re.sub(r":([^:@/]+)@", r":***@", str(proxy_url))


def load_proxy_list(
    proxy: Optional[Union[str, List[str]]] = None,
    proxy_file: Optional[str] = None
) -> List[str]:
    """
    Loads and normalizes proxies from:
    1. Direct arguments (string, comma-separated, or list)
    2. Explicit proxy file or default proxies.txt / proxy.txt
    3. Environment variables (PROXIES, PROXY_LIST, PROXY_FILE, PROXIES_FILE)
    """
    proxies: List[str] = []

    # 1. From direct input argument
    if proxy:
        if isinstance(proxy, list):
            for p in proxy:
                parsed = parse_proxy(p)
                if parsed:
                    proxies.append(parsed)
        elif isinstance(proxy, str):
            for item in re.split(r'[\r\n,;]+', proxy):
                parsed = parse_proxy(item)
                if parsed:
                    proxies.append(parsed)

    # 2. From specified or candidate proxy files
    file_candidates = []
    if proxy_file:
        file_candidates.append(proxy_file)
    env_file = os.getenv("PROXY_FILE") or os.getenv("PROXIES_FILE")
    if env_file:
        file_candidates.append(env_file)
    file_candidates.extend(["proxies.txt", "proxy.txt"])

    for fpath in file_candidates:
        if fpath and os.path.isfile(fpath):
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#"):
                            parsed = parse_proxy(line)
                            if parsed:
                                proxies.append(parsed)
                if proxies:
                    break
            except Exception as e:
                logger.warning(f"Error reading proxy file '{fpath}': {e}")

    # 3. From environment variable PROXIES or PROXY_LIST
    if not proxies:
        env_proxies = os.getenv("PROXIES") or os.getenv("PROXY_LIST")
        if env_proxies:
            for item in re.split(r'[\r\n,;]+', env_proxies):
                parsed = parse_proxy(item)
                if parsed:
                    proxies.append(parsed)

    # Deduplicate while preserving order
    seen = set()
    unique_proxies = []
    for p in proxies:
        if p not in seen:
            seen.add(p)
            unique_proxies.append(p)

    return unique_proxies


def get_random_proxy(
    proxy: Optional[Union[str, List[str]]] = None,
    proxy_file: Optional[str] = None,
    proxy_list: Optional[List[str]] = None
) -> Optional[str]:
    """Selects a random proxy from the available list."""
    candidates = proxy_list if proxy_list is not None else load_proxy_list(proxy=proxy, proxy_file=proxy_file)
    if candidates:
        return random.choice(candidates)
    return None


# ==========================================
# SCRAPER LOGIC
# ==========================================

def create_scraper_session(proxy: Optional[str] = None):
    """Creates an HTTP session with Chrome TLS fingerprinting and optional proxy configuration."""
    proxies_dict = None
    if proxy:
        normalized_proxy = parse_proxy(proxy) or proxy
        proxies_dict = {
            "http": normalized_proxy,
            "https": normalized_proxy
        }
        logger.info(f"Configuring session with proxy: {mask_proxy_url(normalized_proxy)}")
    else:
        logger.info("Configuring session without proxy (direct connection).")

    if HAS_CURL_CFFI:
        logger.info("Using curl_cffi with Chrome impersonation (bypasses Cloudflare 403).")
        if proxies_dict:
            session = curl_requests.Session(impersonate="chrome120", proxies=proxies_dict)
        else:
            session = curl_requests.Session(impersonate="chrome120")
        session.headers.update(DEFAULT_HEADERS)
        return session
    else:
        logger.warning("curl_cffi is NOT installed. Falling back to standard requests (may get 403). Run: pip install curl_cffi")
        session = requests.Session()
        if proxies_dict:
            session.proxies.update(proxies_dict)
        session.headers.update(DEFAULT_HEADERS)
        retry_strategy = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET"]
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        return session


def fetch_user_comments(
    session,
    user_id: int,
    max_pages: int = MAX_PAGES_PER_USER,
    user_name: str = ""
) -> Tuple[List[Dict[str, Any]], bool]:
    """
    Fetches comments for a user across pages.
    Returns: (list_of_comments, is_successful)
    """
    all_comments = []
    seen_comment_ids = set()
    success = True

    for page in range(1, max_pages + 1):
        if page == 1:
            url = f"{BASE_URL}/members/{user_id}/comments"
        else:
            url = f"{BASE_URL}/members/{user_id}/comments/{page}"

        logger.info(f"Fetching User {user_id} - Page {page}/{max_pages}: {url}")

        try:
            response = session.get(url, timeout=REQUEST_TIMEOUT)
            
            if response.status_code == 404:
                logger.warning(f"User {user_id} page {page} returned 404 Not Found.")
                break
            elif response.status_code == 403:
                logger.error(f"User {user_id} page {page} returned HTTP 403 Forbidden (Cloudflare bot protection).")
                success = False
                break
            elif response.status_code != 200:
                logger.error(f"User {user_id} page {page} returned HTTP {response.status_code}")
                success = False
                break

            page_comments = extract_comments_from_html(
                response.text,
                base_url=BASE_URL,
                user_id=user_id,
                user_name=user_name
            )

            if not page_comments:
                logger.info(f"No comments found on page {page} for user {user_id}.")
                if page == 1:
                    break
            else:
                new_page_count = 0
                for c in page_comments:
                    if c['id'] not in seen_comment_ids:
                        seen_comment_ids.add(c['id'])
                        all_comments.append(c)
                        new_page_count += 1
                logger.info(f"Extracted {new_page_count} comments from page {page} (Total unique so far: {len(all_comments)})")

            if "pagination" not in response.text and page > 1:
                break

        except Exception as e:
            logger.error(f"Network error fetching user {user_id} page {page}: {e}")
            success = False
            break

        if page < max_pages:
            delay = random.uniform(REQUEST_DELAY_MIN, REQUEST_DELAY_MAX)
            time.sleep(delay)

    return all_comments, success


def process_user(
    db: DatabaseManager,
    session,
    user_id: int,
    max_pages: int,
    user_name: str = "",
    dry_run: bool = False
) -> Dict[str, Any]:
    # If user_name not supplied, fetch it directly from dbo.users table
    if not user_name:
        user_name = db.get_user_name(user_id)

    name_display = f" (Name: '{user_name}')" if user_name else ""
    logger.info(f"--- Starting processing for User ID: {user_id}{name_display} ---")
    result = {"user_id": user_id, "user_name": user_name, "found": 0, "saved": 0, "success": False}
    try:
        comments, success = fetch_user_comments(session, user_id, max_pages=max_pages, user_name=user_name)
        result["found"] = len(comments)
        result["success"] = success

        if not success:
            logger.warning(f"Request failed for User {user_id}.")
            return result

        if dry_run:
            logger.info(f"[DRY-RUN] Found {len(comments)} primary comments for User {user_id} (User Name: '{user_name}'):")
            for idx, c in enumerate(comments, 1):
                preview = (c.get('comment_text', '')[:70] + '...') if len(c.get('comment_text', '')) > 70 else c.get('comment_text', '')
                logger.info(f"  {idx}. ID: {c['id']} | User: {c.get('user_name')} | URL: {c['url']} | Text: \"{preview}\"")
            return result

        if comments:
            inserted_count = db.insert_comment_urls(comments)
            result["saved"] = inserted_count
            logger.info(f"Saved {inserted_count} comment URLs to database (out of {len(comments)} found) for User {user_id}")
        else:
            logger.info(f"No comments found for User {user_id}.")

    except Exception as e:
        logger.exception(f"Unexpected error processing User {user_id}: {e}")
        result["error"] = str(e)

    return result


def run_scraper(
    user_id: Optional[Union[int, str]] = None,
    user_name: Optional[str] = None,
    users: Optional[List[Dict[str, Any]]] = None,
    pages: Optional[int] = None,
    limit: Optional[int] = None,
    dry_run: bool = False,
    proxy: Optional[Union[str, List[str]]] = None,
    proxy_file: Optional[str] = None,
    progress_callback: Optional[Any] = None
) -> Dict[str, Any]:
    """
    Executes the scraper programmatically and returns a summary dictionary.

    If 'users' or 'user_id' is provided:
        - Runs the scraper ONLY for the specified user(s) provided in the request body/args.
        - Does NOT query or take user IDs from the database 'users' table.
    If no user(s) provided:
        - Fetches user list from InvestingDB.dbo.users table and runs for all users in the DB.
    """
    max_pages = pages if (pages is not None and pages > 0) else MAX_PAGES_PER_USER

    # Load and randomly select a proxy if proxies are configured
    available_proxies = load_proxy_list(proxy=proxy, proxy_file=proxy_file)
    selected_proxy = None
    if available_proxies:
        selected_proxy = random.choice(available_proxies)
        logger.info(f"Loaded {len(available_proxies)} proxy/proxies. Randomly selected proxy for this run: {mask_proxy_url(selected_proxy)}")
    else:
        logger.info("No proxy configured or provided. Running with direct connection.")

    session = create_scraper_session(proxy=selected_proxy)
    db = DatabaseManager()

    start_time = time.time()
    summary = {
        "status": "running",
        "dry_run": dry_run,
        "max_pages": max_pages,
        "proxy": mask_proxy_url(selected_proxy) if selected_proxy else None,
        "total_proxies_available": len(available_proxies),
        "users_total": 0,
        "users_processed": 0,
        "comments_found": 0,
        "comments_saved": 0,
        "errors": [],
        "start_time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(start_time)),
        "end_time": None,
        "duration_seconds": 0
    }

    try:
        # 1. Determine if a specific user / users were targeted in request
        user_was_specified = bool(
            users or
            (user_id is not None and str(user_id).strip()) or
            (user_name is not None and str(user_name).strip())
        )
        target_users = []

        if users:
            raw_list = users if isinstance(users, list) else [users]
            for u in raw_list:
                if isinstance(u, dict):
                    raw_id = u.get('id') if u.get('id') is not None else (u.get('user_id') if u.get('user_id') is not None else u.get('userId'))
                    uname = str(u.get('user_name') or u.get('username') or u.get('name') or u.get('user') or '').strip()
                    if raw_id is not None and str(raw_id).strip():
                        try:
                            uid = int(str(raw_id).strip())
                            target_users.append({'id': uid, 'user_name': uname})
                        except (ValueError, TypeError):
                            logger.warning(f"Skipping invalid user ID in users list: {raw_id}")
                    elif uname:
                        db_user = db.get_user_by_name(uname)
                        if db_user:
                            target_users.append(db_user)
                        else:
                            err_msg = f"User '{uname}' not found in database to resolve numeric user_id."
                            logger.warning(err_msg)
                            summary["errors"].append({"user_name": uname, "error": err_msg})
                elif isinstance(u, int) or (isinstance(u, str) and u.strip().isdigit()):
                    target_users.append({'id': int(str(u).strip()), 'user_name': ''})
                elif isinstance(u, str) and u.strip():
                    uname = u.strip()
                    db_user = db.get_user_by_name(uname)
                    if db_user:
                        target_users.append(db_user)
                    else:
                        err_msg = f"User '{uname}' not found in database to resolve numeric user_id."
                        logger.warning(err_msg)
                        summary["errors"].append({"user_name": uname, "error": err_msg})

        elif (user_id is not None and str(user_id).strip()) or (user_name is not None and str(user_name).strip()):
            uid = None
            uname = str(user_name or '').strip()
            if user_id is not None and str(user_id).strip():
                try:
                    uid = int(str(user_id).strip())
                except (ValueError, TypeError):
                    logger.warning(f"Invalid user_id provided: {user_id}")
            
            if uid is not None:
                target_users.append({'id': uid, 'user_name': uname})
            elif uname:
                db_user = db.get_user_by_name(uname)
                if db_user:
                    target_users.append(db_user)
                else:
                    err_msg = f"User '{uname}' not found in database to resolve numeric user_id."
                    logger.warning(err_msg)
                    summary["errors"].append({"user_name": uname, "error": err_msg})

        if user_was_specified:
            if not target_users:
                logger.warning("User(s) were specified in the request but could not be resolved. Will NOT run for database users.")
                summary["status"] = "failed"
                return summary

            # RUN ONLY FOR SPECIFIED USERS IN BODY / ARGS (BYPASS USERS TABLE QUERY)
            if dry_run:
                logger.info("Running in DRY-RUN mode. Database updates are disabled.")

            summary["users_total"] = len(target_users)
            logger.info(f"Targeting {len(target_users)} specified user(s) directly from request payload (skipping dbo.users query): {target_users}")

            for i, user_info in enumerate(target_users, 1):
                u_id = user_info['id']
                u_name = user_info.get('user_name', '')
                logger.info(f"Progress: [{i}/{len(target_users)}] (User ID: {u_id}, Name: '{u_name}')")

                if progress_callback:
                    progress_callback(i, len(target_users), u_id, u_name)

                res = process_user(db, session, user_id=u_id, max_pages=max_pages, user_name=u_name, dry_run=dry_run)
                summary["users_processed"] += 1
                summary["comments_found"] += res.get("found", 0)
                summary["comments_saved"] += res.get("saved", 0)
                if res.get("error"):
                    summary["errors"].append({"user_id": u_id, "error": res["error"]})

                if i < len(target_users):
                    delay = random.uniform(REQUEST_DELAY_MIN, REQUEST_DELAY_MAX)
                    time.sleep(delay)
        else:
            # NO USER SPECIFIED IN REQUEST -> FETCH USERS FROM DATABASE USERS TABLE
            if dry_run:
                logger.info("Running in DRY-RUN mode. Database updates are disabled.")

            logger.info("No specific user in request. Connecting to database to fetch users from dbo.users...")
            users_from_db = db.get_users(limit=limit)

            if not users_from_db:
                logger.info("No users found in InvestingDB.dbo.users table.")
                summary["status"] = "completed"
                return summary

            summary["users_total"] = len(users_from_db)
            logger.info(f"Found {len(users_from_db)} user(s) in database to scrape.")

            for i, user_info in enumerate(users_from_db, 1):
                u_id = user_info['id']
                u_name = user_info.get('user_name', '')
                logger.info(f"Progress: [{i}/{len(users_from_db)}] (User ID: {u_id}, Name: '{u_name}')")
                
                if progress_callback:
                    progress_callback(i, len(users_from_db), u_id, u_name)

                res = process_user(db, session, user_id=u_id, max_pages=max_pages, user_name=u_name, dry_run=dry_run)
                summary["users_processed"] += 1
                summary["comments_found"] += res.get("found", 0)
                summary["comments_saved"] += res.get("saved", 0)
                if res.get("error"):
                    summary["errors"].append({"user_id": u_id, "error": res["error"]})

                if i < len(users_from_db):
                    delay = random.uniform(REQUEST_DELAY_MIN, REQUEST_DELAY_MAX)
                    time.sleep(delay)

        summary["status"] = "completed"
        logger.info("Scraping completed successfully.")

    except Exception as e:
        logger.exception(f"Fatal scraper error: {e}")
        summary["status"] = "failed"
        summary["errors"].append({"fatal": str(e)})
    finally:
        end_time = time.time()
        summary["end_time"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(end_time))
        summary["duration_seconds"] = round(end_time - start_time, 2)
        db.close()
        if hasattr(session, 'close'):
            session.close()

    return summary


def main():
    parser = argparse.ArgumentParser(description="Scrape comment URLs from Investing.com users and store into SQL database.")
    parser.add_argument("--user-id", type=str, help="Single user ID to scrape directly.")
    parser.add_argument("--user-name", type=str, help="User name corresponding to --user-id.")
    parser.add_argument("--pages", type=int, default=MAX_PAGES_PER_USER, help=f"Max pages to scrape per user (default: {MAX_PAGES_PER_USER}).")
    parser.add_argument("--limit", type=int, help="Limit number of users to fetch from database.")
    parser.add_argument("--dry-run", action="store_true", help="Scrape and display comment URLs without writing to database.")
    parser.add_argument("--proxy", type=str, help="Proxy or comma-separated proxies (e.g., username:password.218.218.252:5432).")
    parser.add_argument("--proxy-file", type=str, help="Path to text file containing proxy list (one proxy per line).")
    args = parser.parse_args()

    run_scraper(
        user_id=args.user_id,
        user_name=args.user_name,
        pages=args.pages,
        limit=args.limit,
        dry_run=args.dry_run,
        proxy=args.proxy,
        proxy_file=args.proxy_file
    )


if __name__ == "__main__":
    main()


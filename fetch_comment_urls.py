import os
import sys
import time
import random
import logging
import argparse
import re
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

try:
    import pyodbc
    HAS_PYODBC = True
except ImportError:
    HAS_PYODBC = False

try:
    import pymssql
    HAS_PYMSSQL = True
except ImportError:
    HAS_PYMSSQL = False

# ==========================================
# CONFIGURATION
# ==========================================

DB_CONFIG = {
    'driver': os.getenv('DB_DRIVER', '{ODBC Driver 17 for SQL Server}'),
    'server': os.getenv('DB_SERVER', 'localhost'),
    'database': os.getenv('DB_NAME', 'InvestingDB'),
    'username': os.getenv('DB_USER', ''),
    'password': os.getenv('DB_PASSWORD', ''),
    'trusted_connection': os.getenv('DB_TRUSTED_CONNECTION', 'yes'),
    'encrypt': os.getenv('DB_ENCRYPT', 'no'),
    'trust_server_certificate': os.getenv('DB_TRUST_CERT', 'yes'),
}

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

# ==========================================
# LOGGING SETUP
# ==========================================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('comment_scraper.log', encoding='utf-8')
    ]
)
logger = logging.getLogger("InvestingScraper")

# ==========================================
# HTML PARSER LOGIC
# ==========================================

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
        if not raw_href or 'comment=' not in raw_href:
            continue

        # Ignore subcomments / reply comments containing '#' (e.g. ?comment=123#456)
        if '#' in raw_href:
            continue

        full_url = urljoin(base_url, raw_href)

        # Extract comment ID
        comment_id = None
        match_param = re.search(r'comment=(\d+)', raw_href)
        if match_param:
            comment_id = int(match_param.group(1))

        # Extract exact comment text
        comment_text = ""
        text_div = body.find('div', id=lambda x: x and x.startswith('commentText-'))
        if text_div:
            comment_text = text_div.get_text(separator=' ', strip=True)
            if not comment_id:
                id_match = re.search(r'commentText-(\d+)', text_div.get('id', ''))
                if id_match:
                    comment_id = int(id_match.group(1))

        if not comment_id or comment_id in seen_ids:
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
            if '#' in raw_href or 'comment=' not in raw_href:
                continue

            comment_id = int(c_id)
            if comment_id in seen_ids:
                continue
            seen_ids.add(comment_id)
            comments.append({
                'id': comment_id,
                'url': urljoin(base_url, raw_href),
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

        if '#' in raw_href or 'comment=' not in raw_href:
            continue

        full_url = urljoin(base_url, raw_href)

        param_match = re.search(r'comment=(\d+)', raw_href)
        if param_match:
            comment_id = int(param_match.group(1))
        else:
            id_match = re.search(r'id=["\']commentText-(\d+)["\']', block)
            if id_match:
                comment_id = int(id_match.group(1))
            else:
                continue

        if comment_id in seen_ids:
            continue

        # Extract text from commentText div
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
# DATABASE MANAGER
# ==========================================

class DatabaseManager:
    def __init__(self, config: Dict[str, Any] = DB_CONFIG):
        self.config = config
        self.conn = None
        self._ensured_columns = False
        self._user_name_col_cached = None

    def _get_best_driver(self) -> str:
        if not HAS_PYODBC:
            return self.config.get('driver', '{ODBC Driver 17 for SQL Server}')
        
        configured_driver = self.config.get('driver', '').strip('{}')
        available_drivers = pyodbc.drivers()
        
        if configured_driver in available_drivers:
            return f"{{{configured_driver}}}"
        
        priority_drivers = [
            "ODBC Driver 18 for SQL Server",
            "ODBC Driver 17 for SQL Server",
            "ODBC Driver 13 for SQL Server",
            "ODBC Driver 11 for SQL Server",
            "SQL Server Native Client 11.0",
            "SQL Server Native Client 10.0",
            "SQL Server"
        ]
        for drv in priority_drivers:
            if drv in available_drivers:
                return f"{{{drv}}}"
        
        return self.config.get('driver', '{ODBC Driver 17 for SQL Server}')

    def get_connection(self):
        if self.conn:
            try:
                cursor = self.conn.cursor()
                cursor.execute("SELECT 1")
                cursor.fetchone()
                return self.conn
            except Exception:
                self.close()

        if HAS_PYODBC:
            conn_str = self._build_pyodbc_conn_str()
            logger.info("Connecting to SQL Server via pyodbc...")
            self.conn = pyodbc.connect(conn_str, autocommit=False)
            self._ensure_schema_columns()
            return self.conn
        elif HAS_PYMSSQL:
            logger.info("Connecting to SQL Server via pymssql...")
            self.conn = pymssql.connect(
                server=self.config.get('server', 'localhost'),
                user=self.config.get('username', ''),
                password=self.config.get('password', ''),
                database=self.config.get('database', 'InvestingDB'),
                autocommit=False
            )
            self._ensure_schema_columns()
            return self.conn
        else:
            raise RuntimeError("Neither 'pyodbc' nor 'pymssql' is installed. Please run: pip install pyodbc")

    def _ensure_schema_columns(self):
        """Ensures the Comments and user_name columns exist in dbo.comment_urls table."""
        if self._ensured_columns or not self.conn:
            return
        try:
            cursor = self.conn.cursor()
            # Ensure Comments column in comment_urls
            cursor.execute("""
            IF NOT EXISTS (
                SELECT 1 FROM sys.columns 
                WHERE object_id = OBJECT_ID('dbo.comment_urls') AND name = 'Comments'
            )
            BEGIN
                ALTER TABLE dbo.comment_urls ADD Comments NVARCHAR(MAX);
            END
            """)
            # Ensure user_name column in comment_urls
            cursor.execute("""
            IF NOT EXISTS (
                SELECT 1 FROM sys.columns 
                WHERE object_id = OBJECT_ID('dbo.comment_urls') AND name = 'user_name'
            )
            BEGIN
                ALTER TABLE dbo.comment_urls ADD user_name NVARCHAR(MAX);
            END
            """)
            self.conn.commit()
            cursor.close()
            self._ensured_columns = True
        except Exception as e:
            logger.debug(f"Note on column schema checks: {e}")

    def _get_user_name_column(self) -> Optional[str]:
        """Dynamically identifies the column name for user name in dbo.users table."""
        if self._user_name_col_cached is not None:
            return self._user_name_col_cached

        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute("""
                SELECT COLUMN_NAME 
                FROM INFORMATION_SCHEMA.COLUMNS 
                WHERE TABLE_NAME = 'users'
            """)
            cols = [row[0].lower() for row in cursor.fetchall()]
            cursor.close()
            for candidate in ['user_name', 'username', 'name', 'profile_name', 'profilename', 'display_name', 'user']:
                if candidate in cols:
                    self._user_name_col_cached = candidate
                    return self._user_name_col_cached
        except Exception as e:
            logger.debug(f"Could not determine user name column in users table: {e}")

        self._user_name_col_cached = None
        return None

    def get_user_name(self, user_id: int) -> str:
        """Fetches the user name for a given user ID from InvestingDB.dbo.users table."""
        name_col = self._get_user_name_column() or 'user_name'
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute(f"SELECT {name_col} FROM InvestingDB.dbo.users WHERE id = ?", (user_id,))
            row = cursor.fetchone()
            cursor.close()
            if row and row[0] is not None:
                return str(row[0]).strip()
        except Exception as e:
            logger.debug(f"Could not fetch user name from users table for user_id={user_id}: {e}")
        return ""

    def get_user_by_name(self, user_name: str) -> Optional[Dict[str, Any]]:
        """Fetches user (id and name) for a given user name from InvestingDB.dbo.users table."""
        if not user_name or not str(user_name).strip():
            return None
        cleaned_name = str(user_name).strip()
        name_col = self._get_user_name_column() or 'user_name'
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute(
                f"SELECT id, {name_col} FROM InvestingDB.dbo.users WHERE LOWER(LTRIM(RTRIM({name_col}))) = LOWER(?)",
                (cleaned_name,)
            )
            row = cursor.fetchone()
            cursor.close()
            if row and row[0] is not None:
                u_id = int(row[0])
                u_name = str(row[1]).strip() if (len(row) > 1 and row[1] is not None) else cleaned_name
                return {'id': u_id, 'user_name': u_name}
        except Exception as e:
            logger.debug(f"Could not fetch user by name '{cleaned_name}' from users table: {e}")
        return None

    def _build_pyodbc_conn_str(self) -> str:
        driver = self._get_best_driver()
        server = self.config.get('server', 'localhost')
        database = self.config.get('database', 'InvestingDB')
        
        parts = [
            f"DRIVER={driver}",
            f"SERVER={server}",
            f"DATABASE={database}",
        ]
        
        user = (self.config.get('username') or '').strip()
        password = (self.config.get('password') or '').strip()
        trusted = (self.config.get('trusted_connection') or '').strip().lower()

        if user and password:
            parts.append(f"UID={user}")
            parts.append(f"PWD={password}")
        elif trusted in ('yes', '1', 'true'):
            parts.append("Trusted_Connection=yes")

        if self.config.get('encrypt'):
            parts.append(f"Encrypt={self.config['encrypt']}")
        if self.config.get('trust_server_certificate'):
            parts.append(f"TrustServerCertificate={self.config['trust_server_certificate']}")

        return ";".join(parts) + ";"

    def get_users(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """
        Fetches users from InvestingDB.dbo.users table status-independently (without filtering by status).
        Returns a list of dicts: [{'id': <int>, 'user_name': <str>}, ...]
        """
        conn = self.get_connection()
        cursor = conn.cursor()
        
        name_col = self._get_user_name_column()
        select_cols = f"id, {name_col}" if name_col else "id"
        top_clause = f"TOP {int(limit)} " if limit else ""
        query = f"SELECT {top_clause}{select_cols} FROM InvestingDB.dbo.users"
            
        cursor.execute(query)
        rows = cursor.fetchall()
        users = []
        for row in rows:
            u_id = int(row[0])
            u_name = str(row[1]).strip() if (name_col and len(row) > 1 and row[1] is not None) else ""
            users.append({'id': u_id, 'user_name': u_name})
        cursor.close()
        return users

    def get_unprocessed_users(self, limit: Optional[int] = None) -> List[int]:
        """Backward compatibility helper that returns list of user IDs."""
        return [u['id'] for u in self.get_users(limit=limit)]

    def insert_comment_urls(self, comments: List[Dict[str, Any]]) -> int:
        if not comments:
            return 0

        conn = self.get_connection()
        cursor = conn.cursor()
        inserted_count = 0

        # Check if 'id' is an IDENTITY column
        is_identity = False
        try:
            cursor.execute("SELECT is_identity FROM sys.columns WHERE object_id = OBJECT_ID('dbo.comment_urls') AND name = 'id'")
            row = cursor.fetchone()
            if row and row[0] == 1:
                is_identity = True
        except Exception:
            pass

        insert_without_id_sql = """
        IF NOT EXISTS (SELECT 1 FROM dbo.comment_urls WHERE (user_id = ? AND url = ?))
        BEGIN
            INSERT INTO dbo.comment_urls (url, user_id, status, fetched_at, Comments, user_name)
            VALUES (?, ?, N'not processed', SYSDATETIME(), ?, ?);
        END
        """

        insert_plain_sql = """
        IF NOT EXISTS (SELECT 1 FROM dbo.comment_urls WHERE id = ? OR (user_id = ? AND url = ?))
        BEGIN
            INSERT INTO dbo.comment_urls (id, url, user_id, status, fetched_at, Comments, user_name)
            VALUES (?, ?, ?, N'not processed', SYSDATETIME(), ?, ?);
        END
        """

        for c in comments:
            comment_text = c.get('comment_text', '')
            user_name = c.get('user_name', '')
            try:
                if is_identity:
                    cursor.execute(insert_without_id_sql, (
                        c['user_id'], c['url'],
                        c['url'], c['user_id'], comment_text, user_name
                    ))
                else:
                    cursor.execute(insert_plain_sql, (
                        c['id'], c['user_id'], c['url'],
                        c['id'], c['url'], c['user_id'], comment_text, user_name
                    ))

                if cursor.rowcount > 0:
                    inserted_count += 1
            except Exception as e:
                err_str = str(e)
                if '544' in err_str:
                    try:
                        cursor.execute(insert_without_id_sql, (
                            c['user_id'], c['url'],
                            c['url'], c['user_id'], comment_text, user_name
                        ))
                        if cursor.rowcount > 0:
                            inserted_count += 1
                        is_identity = True
                        continue
                    except Exception as fallback_err:
                        logger.warning(f"Error inserting comment URL {c.get('url')}: {fallback_err}")
                else:
                    logger.warning(f"Error inserting comment id={c.get('id')}: {e}")

        conn.commit()
        cursor.close()
        return inserted_count

    def close(self):
        if self.conn:
            try:
                self.conn.close()
            except Exception:
                pass
            self.conn = None

# ==========================================
# SCRAPER LOGIC
# ==========================================

def create_scraper_session():
    """Creates an HTTP session with Chrome TLS fingerprinting if curl_cffi is available."""
    if HAS_CURL_CFFI:
        logger.info("Using curl_cffi with Chrome impersonation (bypasses Cloudflare 403).")
        session = curl_requests.Session(impersonate="chrome120")
        session.headers.update(DEFAULT_HEADERS)
        return session
    else:
        logger.warning("curl_cffi is NOT installed. Falling back to standard requests (may get 403). Run: pip install curl_cffi")
        session = requests.Session()
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
    session = create_scraper_session()
    db = DatabaseManager()

    start_time = time.time()
    summary = {
        "status": "running",
        "dry_run": dry_run,
        "max_pages": max_pages,
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
    args = parser.parse_args()

    run_scraper(
        user_id=args.user_id,
        user_name=args.user_name,
        pages=args.pages,
        limit=args.limit,
        dry_run=args.dry_run
    )


if __name__ == "__main__":
    main()


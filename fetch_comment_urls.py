import os
import sys
import time
import random
import logging
import argparse
import re
from typing import List, Dict, Any, Optional, Tuple
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
MAX_PAGES_PER_USER = int(os.getenv('MAX_PAGES_PER_USER', '2'))
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

def extract_comments_from_html(html_content: str, base_url: str = BASE_URL, user_id: int = 0) -> List[Dict[str, Any]]:
    if not html_content:
        return []

    if HAS_BS4:
        return _extract_with_bs4(html_content, base_url, user_id)
    else:
        return _extract_with_regex(html_content, base_url, user_id)


def _extract_with_bs4(html_content: str, base_url: str, user_id: int) -> List[Dict[str, Any]]:
    soup = BeautifulSoup(html_content, 'html.parser')
    comments = []
    seen_ids = set()

    # Extract user name
    user_name = ""
    h1_tag = soup.find('h1')
    if h1_tag:
        user_name = h1_tag.get_text(strip=True)
    if not user_name:
        og_title = soup.find('meta', property='og:title') or soup.find('meta', attrs={'name': 'twitter:title'})
        if og_title and og_title.get('content'):
            user_name = re.sub(r'^Comments by\s+', '', og_title.get('content', ''), flags=re.IGNORECASE).strip()
    if not user_name and soup.title:
        user_name = re.sub(r'^Comments by\s+', '', soup.title.get_text(strip=True), flags=re.IGNORECASE).strip()

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


def _extract_with_regex(html_content: str, base_url: str, user_id: int) -> List[Dict[str, Any]]:
    comments = []
    seen_ids = set()

    # Extract user name
    user_name = ""
    h1_match = re.search(r'<h1>(.*?)</h1>', html_content, re.IGNORECASE)
    if h1_match:
        user_name = re.sub(r'<[^>]+>', '', h1_match.group(1)).strip()
    if not user_name:
        title_match = re.search(r'<title>(.*?)</title>', html_content, re.IGNORECASE)
        if title_match:
            user_name = re.sub(r'^Comments by\s+', '', title_match.group(1), flags=re.IGNORECASE).strip()

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
            # Ensure Comments column
            cursor.execute("""
            IF NOT EXISTS (
                SELECT 1 FROM sys.columns 
                WHERE object_id = OBJECT_ID('dbo.comment_urls') AND name = 'Comments'
            )
            BEGIN
                ALTER TABLE dbo.comment_urls ADD Comments NVARCHAR(MAX);
            END
            """)
            # Ensure user_name column
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

    def get_unprocessed_users(self, limit: Optional[int] = None) -> List[int]:
        conn = self.get_connection()
        cursor = conn.cursor()
        
        query = "SELECT id FROM InvestingDB.dbo.users WHERE status IN ('unprocessed', 'not processed', 'pending', 'new')"
        if limit:
            query = f"SELECT TOP {int(limit)} id FROM InvestingDB.dbo.users WHERE status IN ('unprocessed', 'not processed', 'pending', 'new')"
            
        cursor.execute(query)
        rows = cursor.fetchall()
        user_ids = [int(row[0]) for row in rows]
        cursor.close()
        return user_ids

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

    def update_user_status(self, user_id: int, status: str) -> None:
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            update_sql = "UPDATE InvestingDB.dbo.users SET status = ? WHERE id = ?"
            cursor.execute(update_sql, (status, user_id))
            conn.commit()
        finally:
            cursor.close()

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
    max_pages: int = MAX_PAGES_PER_USER
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

            page_comments = extract_comments_from_html(response.text, base_url=BASE_URL, user_id=user_id)

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
    dry_run: bool = False
) -> None:
    logger.info(f"--- Starting processing for User ID: {user_id} ---")
    try:
        comments, success = fetch_user_comments(session, user_id, max_pages=max_pages)

        if not success:
            logger.warning(f"Request failed for User {user_id}. Updating status to 'failed'")
            if not dry_run:
                db.update_user_status(user_id, 'failed')
            return

        if dry_run:
            name_str = comments[0].get('user_name', '') if comments else ''
            logger.info(f"[DRY-RUN] Found {len(comments)} primary comments for User {user_id} (User Name: '{name_str}'):")
            for idx, c in enumerate(comments, 1):
                preview = (c.get('comment_text', '')[:70] + '...') if len(c.get('comment_text', '')) > 70 else c.get('comment_text', '')
                logger.info(f"  {idx}. ID: {c['id']} | User: {c.get('user_name')} | URL: {c['url']} | Text: \"{preview}\"")
            return

        if comments:
            inserted_count = db.insert_comment_urls(comments)
            logger.info(f"Saved {inserted_count} new comment URLs to database (out of {len(comments)} found) for User {user_id}")
            db.update_user_status(user_id, 'processed')
        else:
            logger.info(f"No comments found for User {user_id}. Updating status to 'no_comments'")
            db.update_user_status(user_id, 'no_comments')

    except Exception as e:
        logger.exception(f"Unexpected error processing User {user_id}: {e}")
        if not dry_run:
            try:
                db.update_user_status(user_id, 'failed')
            except Exception:
                pass


def main():
    parser = argparse.ArgumentParser(description="Scrape comment URLs from Investing.com users and store into SQL database.")
    parser.add_argument("--user-id", type=int, help="Single user ID to scrape directly.")
    parser.add_argument("--pages", type=int, default=MAX_PAGES_PER_USER, help=f"Max pages to scrape per user (default: {MAX_PAGES_PER_USER}).")
    parser.add_argument("--limit", type=int, help="Limit number of unprocessed users to fetch from database.")
    parser.add_argument("--dry-run", action="store_true", help="Scrape and display comment URLs without writing to database.")
    parser.add_argument("--retry-failed", action="store_true", help="Also retry users marked as 'failed' or 'no_comments'.")
    args = parser.parse_args()

    session = create_scraper_session()
    db = DatabaseManager()

    try:
        if args.user_id:
            process_user(db, session, args.user_id, max_pages=args.pages, dry_run=args.dry_run)
        else:
            if args.dry_run:
                logger.info("Running in DRY-RUN mode. Database updates are disabled.")

            logger.info("Connecting to database to fetch unprocessed users...")
            unprocessed_users = db.get_unprocessed_users(limit=args.limit)

            if not unprocessed_users:
                logger.info("No unprocessed users found in InvestingDB.dbo.users table.")
                return

            logger.info(f"Found {len(unprocessed_users)} unprocessed user(s) to scrape.")

            for i, user_id in enumerate(unprocessed_users, 1):
                logger.info(f"Progress: [{i}/{len(unprocessed_users)}]")
                process_user(db, session, user_id, max_pages=args.pages, dry_run=args.dry_run)

                if i < len(unprocessed_users):
                    delay = random.uniform(REQUEST_DELAY_MIN, REQUEST_DELAY_MAX)
                    time.sleep(delay)

            logger.info("Scraping completed successfully.")

    finally:
        db.close()
        if hasattr(session, 'close'):
            session.close()


if __name__ == "__main__":
    main()

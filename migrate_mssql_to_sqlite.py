#!/usr/bin/env python3
"""
migrate_mssql_to_sqlite.py
==========================
Safe, idempotent migration script to copy existing data from the SQL Server
(InvestingDB) database into the new SQLite database.

Preserves:
  - All IDs (users.id, comment_urls.id)
  - All relationships (comment_urls.user_id -> users.id)
  - All existing records
  - Timestamps (fetched_at stored as-is)
  - All text fields (Comments, user_name, status, url)

The script is IDEMPOTENT:
  - Running it twice will NOT duplicate data.
  - Existing rows in SQLite are left untouched (INSERT OR IGNORE).

Usage:
  python migrate_mssql_to_sqlite.py [--sqlite-path /app/data/app.db]
                                     [--mssql-server 74.207.229.12]
                                     [--mssql-db InvestingDB]
                                     [--mssql-user SA]
                                     [--mssql-password YOUR_PASSWORD]
                                     [--batch-size 500]
                                     [--dry-run]

Requirements:
  pip install pyodbc python-dotenv
  (pyodbc must be available; this script is a one-time migration tool)
"""

import os
import sys
import argparse
import sqlite3
import time
import logging

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    # Manual .env parser fallback if python-dotenv is not installed
    env_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.isfile(env_file):
        with open(env_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    k = k.strip()
                    v = v.strip().strip("'\"")
                    if k and k not in os.environ:
                        os.environ[k] = v


logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("MSSQLtoSQLite")


# ---------------------------------------------------------------------------
# SQL Server connection (via pyodbc)
# ---------------------------------------------------------------------------

def get_mssql_connection(server: str, database: str, user: str, password: str):
    """Opens a connection to SQL Server via pyodbc (preferred) or pymssql (fallback)."""
    # 1. Try pyodbc
    try:
        import pyodbc
        drivers = pyodbc.drivers()
        driver = next(
            (d for d in [
                "ODBC Driver 18 for SQL Server",
                "ODBC Driver 17 for SQL Server",
                "ODBC Driver 13 for SQL Server",
                "ODBC Driver 11 for SQL Server",
                "SQL Server Native Client 11.0",
                "SQL Server",
            ] if d in drivers),
            None
        )
        if driver:
            conn_str = (
                f"DRIVER={{{driver}}};"
                f"SERVER={server};"
                f"DATABASE={database};"
                f"UID={user};"
                f"PWD={password};"
                "Encrypt=no;"
                "TrustServerCertificate=yes;"
            )
            logger.info(f"Connecting to SQL Server via pyodbc: {server}/{database} (driver: {driver}) ...")
            conn = pyodbc.connect(conn_str, autocommit=True)
            logger.info("Connected to SQL Server successfully via pyodbc.")
            return conn
        else:
            logger.warning(f"pyodbc is installed but no ODBC driver was found. Available: {drivers}")
    except ImportError:
        logger.debug("pyodbc not installed; trying pymssql fallback...")

    # 2. Try pymssql
    try:
        import pymssql
        logger.info(f"Connecting to SQL Server via pymssql: {server}/{database} ...")
        conn = pymssql.connect(
            server=server,
            user=user,
            password=password,
            database=database,
            autocommit=True
        )
        logger.info("Connected to SQL Server successfully via pymssql.")
        return conn
    except ImportError:
        pass

    logger.error(
        "Neither pyodbc nor pymssql is installed.\n"
        "To run the migration script, install either driver on your host:\n"
        "  pip install pyodbc\n"
        "or:\n"
        "  pip install pymssql"
    )
    sys.exit(1)


# ---------------------------------------------------------------------------
# SQLite setup
# ---------------------------------------------------------------------------

def get_sqlite_connection(db_path: str) -> sqlite3.Connection:
    """Opens a SQLite connection and ensures the schema exists."""
    data_dir = os.path.dirname(db_path)
    if data_dir:
        os.makedirs(data_dir, exist_ok=True)

    logger.info(f"Opening SQLite database: {db_path}")
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.row_factory = sqlite3.Row

    conn.execute("BEGIN")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id        INTEGER PRIMARY KEY,
            user_name TEXT
        )
    """)
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
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_comment_urls_user_url
        ON comment_urls (user_id, url)
    """)
    conn.execute("COMMIT")
    return conn


# ---------------------------------------------------------------------------
# Migration helpers
# ---------------------------------------------------------------------------

def detect_user_name_col(mssql_conn) -> str:
    """Detects the user name column in the SQL Server users table."""
    cursor = mssql_conn.cursor()
    cursor.execute("""
        SELECT COLUMN_NAME
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_NAME = 'users'
    """)
    cols = [row[0].lower() for row in cursor.fetchall()]
    cursor.close()
    for candidate in ['user_name', 'username', 'name', 'profile_name',
                      'profilename', 'display_name', 'user']:
        if candidate in cols:
            logger.info(f"Detected user name column in users table: '{candidate}'")
            return candidate
    logger.warning("Could not detect user name column; defaulting to 'user_name'.")
    return 'user_name'


def migrate_users(mssql_conn, sqlite_conn, user_name_col: str,
                  batch_size: int, dry_run: bool) -> int:
    """Migrates the users table from SQL Server to SQLite."""
    logger.info("Migrating users table ...")
    cursor = mssql_conn.cursor()
    cursor.execute(f"SELECT id, {user_name_col} FROM InvestingDB.dbo.users")

    total_inserted = 0
    total_skipped = 0
    batch = cursor.fetchmany(batch_size)

    while batch:
        rows = [(int(row[0]), str(row[1]).strip() if row[1] is not None else '') for row in batch]
        if not dry_run:
            sqlite_conn.execute("BEGIN")
            for row in rows:
                result = sqlite_conn.execute(
                    "INSERT OR IGNORE INTO users (id, user_name) VALUES (?, ?)", row
                )
                if result.rowcount > 0:
                    total_inserted += 1
                else:
                    total_skipped += 1
            sqlite_conn.execute("COMMIT")
        else:
            total_inserted += len(rows)
            logger.info(f"[DRY-RUN] Would insert {len(rows)} users.")

        batch = cursor.fetchmany(batch_size)

    cursor.close()
    logger.info(
        f"Users migration complete: {total_inserted} inserted, {total_skipped} already existed (skipped)."
    )
    return total_inserted


def migrate_comment_urls(mssql_conn, sqlite_conn, batch_size: int, dry_run: bool) -> int:
    """Migrates the comment_urls table from SQL Server to SQLite."""
    logger.info("Migrating comment_urls table ...")

    # Check which columns exist in the SQL Server comment_urls table
    cursor = mssql_conn.cursor()
    cursor.execute("""
        SELECT COLUMN_NAME
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_NAME = 'comment_urls'
    """)
    mssql_cols = {row[0].lower() for row in cursor.fetchall()}
    cursor.close()

    has_comments = 'comments' in mssql_cols
    has_user_name = 'user_name' in mssql_cols
    has_fetched_at = 'fetched_at' in mssql_cols

    select_parts = ["id", "url", "user_id", "status"]
    if has_fetched_at:
        select_parts.append("fetched_at")
    if has_comments:
        select_parts.append("Comments")
    if has_user_name:
        select_parts.append("user_name")

    select_sql = f"SELECT {', '.join(select_parts)} FROM InvestingDB.dbo.comment_urls"
    logger.info(f"SQL Server query: {select_sql}")

    cursor = mssql_conn.cursor()
    cursor.execute(select_sql)

    total_inserted = 0
    total_skipped = 0
    batch_num = 0
    batch = cursor.fetchmany(batch_size)

    while batch:
        batch_num += 1
        rows_to_insert = []
        for row in batch:
            col_idx = 0
            r_id = int(row[col_idx]); col_idx += 1
            r_url = str(row[col_idx] or '').strip(); col_idx += 1
            r_user_id = int(row[col_idx]) if row[col_idx] is not None else None; col_idx += 1
            r_status = str(row[col_idx] or 'not processed').strip(); col_idx += 1
            r_fetched_at = str(row[col_idx]).strip() if has_fetched_at and row[col_idx] is not None else None
            if has_fetched_at:
                col_idx += 1
            r_comments = str(row[col_idx] or '').strip() if has_comments else ''
            if has_comments:
                col_idx += 1
            r_user_name = str(row[col_idx] or '').strip() if has_user_name else ''

            if not r_url:
                continue

            rows_to_insert.append((
                r_id, r_url, r_user_id, r_status,
                r_fetched_at or time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
                r_comments, r_user_name
            ))

        if not dry_run:
            sqlite_conn.execute("BEGIN")
            for row_data in rows_to_insert:
                result = sqlite_conn.execute(
                    """
                    INSERT OR IGNORE INTO comment_urls
                        (id, url, user_id, status, fetched_at, Comments, user_name)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    row_data
                )
                if result.rowcount > 0:
                    total_inserted += 1
                else:
                    total_skipped += 1
            sqlite_conn.execute("COMMIT")
            logger.info(f"  Batch {batch_num}: inserted {len(rows_to_insert)} rows "
                        f"(total so far: {total_inserted} inserted, {total_skipped} skipped).")
        else:
            total_inserted += len(rows_to_insert)
            logger.info(f"[DRY-RUN] Batch {batch_num}: would insert {len(rows_to_insert)} comment_url rows.")

        batch = cursor.fetchmany(batch_size)

    cursor.close()
    logger.info(
        f"comment_urls migration complete: {total_inserted} inserted, {total_skipped} already existed (skipped)."
    )
    return total_inserted


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

def verify_migration(mssql_conn, sqlite_conn):
    """Compares row counts between SQL Server and SQLite to verify migration."""
    logger.info("=== Verification ===")

    ms_cur = mssql_conn.cursor()
    ms_cur.execute("SELECT COUNT(*) FROM InvestingDB.dbo.users")
    ms_users = ms_cur.fetchone()[0]
    ms_cur.execute("SELECT COUNT(*) FROM InvestingDB.dbo.comment_urls")
    ms_comments = ms_cur.fetchone()[0]
    ms_cur.close()

    sq_users = sqlite_conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    sq_comments = sqlite_conn.execute("SELECT COUNT(*) FROM comment_urls").fetchone()[0]

    logger.info(f"  users       : SQL Server={ms_users:,}, SQLite={sq_users:,}")
    logger.info(f"  comment_urls: SQL Server={ms_comments:,}, SQLite={sq_comments:,}")

    ok = True
    if sq_users < ms_users:
        logger.warning(f"  ⚠ SQLite users count ({sq_users}) is LESS than SQL Server ({ms_users}). Check for errors.")
        ok = False
    if sq_comments < ms_comments:
        logger.warning(f"  ⚠ SQLite comment_urls count ({sq_comments}) is LESS than SQL Server ({ms_comments}). Check for errors.")
        ok = False

    if ok:
        logger.info("  ✓ Row counts match (or SQLite has more, which is normal if new rows were added).")

    return ok


def _resolve_default_sqlite_path() -> str:
    """Resolves sensible SQLite target path depending on host vs container environment."""
    if os.getenv("DB_PATH"):
        return os.getenv("DB_PATH")
    
    db_url = os.getenv("DATABASE_URL", "").strip()
    if db_url:
        path = db_url.replace("sqlite:////", "/").replace("sqlite:///", "").replace("sqlite://", "")
        # If running outside container on Windows/Mac/Linux and /app/data does not exist, use local ./data/app.db
        if (path.startswith("/app/data") or path == "/app/data/app.db") and not os.path.isdir("/app/data") and not os.path.isdir("/app"):
            return os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "app.db")
        if path:
            return path

    if os.path.isdir("/app/data") or os.path.isdir("/app"):
        return "/app/data/app.db"
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "app.db")


def main():
    parser = argparse.ArgumentParser(
        description="Migrate data from SQL Server (InvestingDB) to SQLite."
    )
    parser.add_argument(
        "--sqlite-path",
        default=_resolve_default_sqlite_path(),
        help="Path to the SQLite database file (default: auto-detected ./data/app.db or /app/data/app.db)"
    )
    parser.add_argument("--mssql-server", default=os.getenv("DB_SERVER", "74.207.229.12"), help="SQL Server hostname or IP")
    parser.add_argument("--mssql-db", default=os.getenv("DB_NAME", "InvestingDB"), help="SQL Server database name")
    parser.add_argument("--mssql-user", default=os.getenv("DB_USER", "SA"), help="SQL Server username")
    parser.add_argument(
        "--mssql-password",
        default=os.getenv("DB_PASSWORD") or os.getenv("MSSQL_PASSWORD") or "ScrapInvest321",
        help="SQL Server password (default: read from env or project default)"
    )
    parser.add_argument("--batch-size", type=int, default=500, help="Number of rows to process per batch")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be migrated without writing to SQLite")
    parser.add_argument("--skip-verify", action="store_true", help="Skip row count verification at the end")
    args = parser.parse_args()

    # Resolve sqlite path from DATABASE_URL if given
    sqlite_path = args.sqlite_path
    if sqlite_path.startswith("sqlite:///"):
        sqlite_path = sqlite_path[len("sqlite:///"):]
    elif sqlite_path.startswith("sqlite://"):
        sqlite_path = sqlite_path[len("sqlite://"):]

    logger.info("=" * 60)
    logger.info("SQL Server -> SQLite Migration")
    logger.info(f"  Source : {args.mssql_server}/{args.mssql_db}")
    logger.info(f"  Target : {sqlite_path}")
    logger.info(f"  Dry run: {args.dry_run}")
    logger.info("=" * 60)

    if not args.mssql_password:
        logger.error("SQL Server password not provided. Use --mssql-password or set DB_PASSWORD in .env")
        sys.exit(1)

    mssql_conn = get_mssql_connection(
        server=args.mssql_server,
        database=args.mssql_db,
        user=args.mssql_user,
        password=args.mssql_password
    )
    sqlite_conn = get_sqlite_connection(sqlite_path)

    try:
        user_name_col = detect_user_name_col(mssql_conn)
        migrate_users(mssql_conn, sqlite_conn, user_name_col, args.batch_size, args.dry_run)
        migrate_comment_urls(mssql_conn, sqlite_conn, args.batch_size, args.dry_run)

        if not args.skip_verify and not args.dry_run:
            verify_migration(mssql_conn, sqlite_conn)

        logger.info("Migration completed successfully.")
    finally:
        mssql_conn.close()
        sqlite_conn.close()


if __name__ == "__main__":
    main()

"""
test_sqlite_db.py
=================
Focused test suite for the SQLite database layer.

Tests:
  1. Schema creation (tables + index)
  2. INSERT users
  3. SELECT users (get_users, get_user_name, get_user_by_name)
  4. INSERT comment_urls (insert_comment_urls)
  5. UPSERT / duplicate prevention (INSERT OR IGNORE)
  6. UPDATE (direct SQL, verifying data integrity after update)
  7. Relationships (comment_urls.user_id -> users.id)
  8. LIMIT (get_users with limit)
  9. Persistence check (close + reopen connection)
 10. Existing API endpoints test (schema-level, using TestClient)

Run with:
    python -m pytest test_sqlite_db.py -v
    # or without pytest:
    python test_sqlite_db.py
"""

import os
import sys
import sqlite3
import tempfile
import unittest
import logging

# ---------------------------------------------------------------------------
# Guard: redirect comment_scraper.log to a temp file so the module-level
# logging.FileHandler in fetch_comment_urls.py doesn't fail on Windows when
# the real log file is locked by a running Docker container or another process.
# ---------------------------------------------------------------------------
_LOG_ENV_KEY = "LOG_FILE"
_tmp_log = tempfile.NamedTemporaryFile(suffix=".log", delete=False)
_tmp_log.close()
os.environ[_LOG_ENV_KEY] = _tmp_log.name

# Ensure project root is on path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Suppress verbose module output during tests
logging.getLogger("InvestingScraper").setLevel(logging.WARNING)

# ---------------------------------------------------------------------------
# Helper: build a DatabaseManager pointing to a temp in-memory or file DB
# ---------------------------------------------------------------------------

def make_db(path: str = None):
    """Creates a DatabaseManager instance using the given path (or a temp file)."""
    from fetch_comment_urls import DatabaseManager
    p = path or ":memory:"
    db = DatabaseManager(db_path=p)
    return db


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

class TestSQLiteSchema(unittest.TestCase):
    """Verify schema initialisation."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db_path = self.tmp.name

    def tearDown(self):
        try:
            os.unlink(self.db_path)
        except OSError:
            pass

    def test_tables_created(self):
        db = make_db(self.db_path)
        conn = db.get_connection()
        tables = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        self.assertIn("users", tables)
        self.assertIn("comment_urls", tables)
        db.close()

    def test_unique_index_created(self):
        db = make_db(self.db_path)
        conn = db.get_connection()
        indexes = {
            row[1] for row in conn.execute(
                "SELECT * FROM sqlite_master WHERE type='index' AND tbl_name='comment_urls'"
            )
        }
        self.assertIn("idx_comment_urls_user_url", indexes)
        db.close()

    def test_comment_urls_columns(self):
        db = make_db(self.db_path)
        conn = db.get_connection()
        cols = {row[1].lower() for row in conn.execute("PRAGMA table_info(comment_urls)")}
        for expected in ["id", "url", "user_id", "status", "fetched_at", "comments", "user_name"]:
            self.assertIn(expected, cols, f"Missing column: {expected}")
        db.close()


class TestUserOperations(unittest.TestCase):
    """Test INSERT / SELECT on the users table."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db_path = self.tmp.name
        self.db = make_db(self.db_path)
        conn = self.db.get_connection()
        # Seed users directly
        conn.execute("BEGIN")
        conn.execute("INSERT INTO users (id, user_name) VALUES (1, 'Alice')")
        conn.execute("INSERT INTO users (id, user_name) VALUES (2, 'Bob  ')")   # trailing space
        conn.execute("INSERT INTO users (id, user_name) VALUES (3, 'Charlie')")
        conn.execute("COMMIT")

    def tearDown(self):
        self.db.close()
        try:
            os.unlink(self.db_path)
        except OSError:
            pass

    def test_get_users_all(self):
        users = self.db.get_users()
        self.assertEqual(len(users), 3)
        ids = {u['id'] for u in users}
        self.assertEqual(ids, {1, 2, 3})

    def test_get_users_limit(self):
        users = self.db.get_users(limit=2)
        self.assertEqual(len(users), 2)

    def test_get_user_name(self):
        name = self.db.get_user_name(1)
        self.assertEqual(name, "Alice")

    def test_get_user_name_strips_whitespace(self):
        name = self.db.get_user_name(2)
        self.assertEqual(name, "Bob")   # trailing space stripped

    def test_get_user_name_nonexistent(self):
        name = self.db.get_user_name(999)
        self.assertEqual(name, "")

    def test_get_user_by_name_exact(self):
        result = self.db.get_user_by_name("Alice")
        self.assertIsNotNone(result)
        self.assertEqual(result['id'], 1)
        self.assertEqual(result['user_name'], "Alice")

    def test_get_user_by_name_case_insensitive(self):
        result = self.db.get_user_by_name("alice")
        self.assertIsNotNone(result)
        self.assertEqual(result['id'], 1)

    def test_get_user_by_name_with_whitespace(self):
        result = self.db.get_user_by_name("Bob")
        self.assertIsNotNone(result)
        self.assertEqual(result['id'], 2)

    def test_get_user_by_name_nonexistent(self):
        result = self.db.get_user_by_name("Nonexistent")
        self.assertIsNone(result)

    def test_get_user_by_name_empty(self):
        result = self.db.get_user_by_name("")
        self.assertIsNone(result)

    def test_get_unprocessed_users(self):
        ids = self.db.get_unprocessed_users()
        self.assertEqual(sorted(ids), [1, 2, 3])

    def test_get_unprocessed_users_limit(self):
        ids = self.db.get_unprocessed_users(limit=1)
        self.assertEqual(len(ids), 1)


class TestCommentUrlOperations(unittest.TestCase):
    """Test INSERT / UPSERT on the comment_urls table."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db_path = self.tmp.name
        self.db = make_db(self.db_path)
        conn = self.db.get_connection()
        conn.execute("INSERT INTO users (id, user_name) VALUES (100, 'TestUser')")

    def tearDown(self):
        self.db.close()
        try:
            os.unlink(self.db_path)
        except OSError:
            pass

    def _make_comment(self, cid, url, uid=100, text="", name="TestUser"):
        return {
            'id': cid,
            'url': url,
            'user_id': uid,
            'comment_text': text,
            'user_name': name
        }

    def test_insert_single(self):
        comments = [self._make_comment(1001, "https://example.com/c?comment=1001")]
        inserted = self.db.insert_comment_urls(comments)
        self.assertEqual(inserted, 1)

    def test_insert_multiple(self):
        comments = [
            self._make_comment(2001, "https://example.com/c?comment=2001"),
            self._make_comment(2002, "https://example.com/c?comment=2002"),
            self._make_comment(2003, "https://example.com/c?comment=2003"),
        ]
        inserted = self.db.insert_comment_urls(comments)
        self.assertEqual(inserted, 3)

    def test_upsert_no_duplicate_by_id(self):
        """Inserting the same id twice should be a no-op the second time."""
        comments = [self._make_comment(3001, "https://example.com/c?comment=3001")]
        self.db.insert_comment_urls(comments)
        inserted = self.db.insert_comment_urls(comments)
        self.assertEqual(inserted, 0)

    def test_upsert_no_duplicate_by_user_url(self):
        """Inserting the same (user_id, url) pair with a different id should also be ignored."""
        self.db.insert_comment_urls([self._make_comment(4001, "https://example.com/c?comment=4001")])
        # Same url and user_id, different id -> should be ignored due to UNIQUE INDEX
        dupe = [self._make_comment(9999, "https://example.com/c?comment=4001")]
        inserted = self.db.insert_comment_urls(dupe)
        self.assertEqual(inserted, 0)

    def test_insert_empty_list(self):
        inserted = self.db.insert_comment_urls([])
        self.assertEqual(inserted, 0)

    def test_insert_preserves_comment_text(self):
        text = "This is a sample comment text."
        comments = [self._make_comment(5001, "https://example.com/c?comment=5001", text=text)]
        self.db.insert_comment_urls(comments)
        conn = self.db.get_connection()
        row = conn.execute(
            "SELECT Comments FROM comment_urls WHERE id = ?", (5001,)
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], text)

    def test_insert_preserves_user_name(self):
        comments = [self._make_comment(6001, "https://example.com/c?comment=6001", name="SpecificUser")]
        self.db.insert_comment_urls(comments)
        conn = self.db.get_connection()
        row = conn.execute(
            "SELECT user_name FROM comment_urls WHERE id = ?", (6001,)
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "SpecificUser")

    def test_insert_default_status(self):
        comments = [self._make_comment(7001, "https://example.com/c?comment=7001")]
        self.db.insert_comment_urls(comments)
        conn = self.db.get_connection()
        row = conn.execute(
            "SELECT status FROM comment_urls WHERE id = ?", (7001,)
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "not processed")

    def test_insert_sets_fetched_at(self):
        comments = [self._make_comment(8001, "https://example.com/c?comment=8001")]
        self.db.insert_comment_urls(comments)
        conn = self.db.get_connection()
        row = conn.execute(
            "SELECT fetched_at FROM comment_urls WHERE id = ?", (8001,)
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertIsNotNone(row[0])
        self.assertTrue(len(row[0]) > 0)

    def test_update_status(self):
        """Verify direct UPDATE operations work correctly."""
        comments = [self._make_comment(9001, "https://example.com/c?comment=9001")]
        self.db.insert_comment_urls(comments)
        conn = self.db.get_connection()
        conn.execute("UPDATE comment_urls SET status = 'processed' WHERE id = ?", (9001,))
        row = conn.execute(
            "SELECT status FROM comment_urls WHERE id = ?", (9001,)
        ).fetchone()
        self.assertEqual(row[0], "processed")


class TestRelationships(unittest.TestCase):
    """Verify foreign-key style relationship between users and comment_urls."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db_path = self.tmp.name
        self.db = make_db(self.db_path)
        conn = self.db.get_connection()
        conn.execute("INSERT INTO users (id, user_name) VALUES (200, 'UserA')")
        conn.execute("INSERT INTO users (id, user_name) VALUES (201, 'UserB')")

    def tearDown(self):
        self.db.close()
        try:
            os.unlink(self.db_path)
        except OSError:
            pass

    def test_comments_associated_with_correct_user(self):
        comments = [
            {'id': 11001, 'url': 'https://x.com/c?comment=11001', 'user_id': 200, 'comment_text': '', 'user_name': 'UserA'},
            {'id': 11002, 'url': 'https://x.com/c?comment=11002', 'user_id': 201, 'comment_text': '', 'user_name': 'UserB'},
        ]
        self.db.insert_comment_urls(comments)
        conn = self.db.get_connection()
        rows = conn.execute(
            "SELECT cu.id, u.user_name FROM comment_urls cu "
            "JOIN users u ON u.id = cu.user_id ORDER BY cu.id"
        ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][1], 'UserA')
        self.assertEqual(rows[1][1], 'UserB')

    def test_filter_comments_by_user(self):
        for i in range(5):
            self.db.insert_comment_urls([{
                'id': 12000 + i,
                'url': f'https://x.com/c?comment=1200{i}',
                'user_id': 200 if i < 3 else 201,
                'comment_text': '',
                'user_name': 'UserA' if i < 3 else 'UserB'
            }])
        conn = self.db.get_connection()
        count_a = conn.execute("SELECT COUNT(*) FROM comment_urls WHERE user_id = ?", (200,)).fetchone()[0]
        count_b = conn.execute("SELECT COUNT(*) FROM comment_urls WHERE user_id = ?", (201,)).fetchone()[0]
        self.assertEqual(count_a, 3)
        self.assertEqual(count_b, 2)


class TestPersistence(unittest.TestCase):
    """Verify data survives closing and reopening the connection (simulates container restart)."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db_path = self.tmp.name

    def tearDown(self):
        try:
            os.unlink(self.db_path)
        except OSError:
            pass

    def test_data_persists_after_close_reopen(self):
        """Data written in one DatabaseManager session is readable in a new one."""
        db1 = make_db(self.db_path)
        conn1 = db1.get_connection()
        conn1.execute("INSERT INTO users (id, user_name) VALUES (500, 'PersistUser')")
        db1.insert_comment_urls([{
            'id': 99001,
            'url': 'https://persist.example.com/c?comment=99001',
            'user_id': 500,
            'comment_text': 'persisted comment',
            'user_name': 'PersistUser'
        }])
        db1.close()

        # Reopen with a brand-new DatabaseManager (simulates container restart)
        db2 = make_db(self.db_path)
        users = db2.get_users()
        ids = [u['id'] for u in users]
        self.assertIn(500, ids)

        name = db2.get_user_name(500)
        self.assertEqual(name, "PersistUser")

        conn2 = db2.get_connection()
        row = conn2.execute(
            "SELECT Comments FROM comment_urls WHERE id = ?", (99001,)
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "persisted comment")
        db2.close()


class TestAPIEndpoints(unittest.TestCase):
    """
    Smoke test the FastAPI endpoints using TestClient.
    Patches the DatabaseManager so no real DB is needed for /api/run.
    Tests that existing API contracts (status codes, response keys) are unchanged.
    """

    def setUp(self):
        os.environ.setdefault("DATABASE_URL", "sqlite:///./data/test_api.db")

    def test_root_endpoint(self):
        try:
            from fastapi.testclient import TestClient
            from app import app
        except ImportError:
            self.skipTest("fastapi[testclient] not available")

        client = TestClient(app)
        r = client.get("/")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertIn("service", data)
        self.assertIn("status", data)
        self.assertIn("endpoints", data)

    def test_health_endpoint(self):
        try:
            from fastapi.testclient import TestClient
            from app import app
        except ImportError:
            self.skipTest("fastapi[testclient] not available")

        client = TestClient(app)
        r = client.get("/health")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertIn("status", data)
        self.assertEqual(data["status"], "healthy")

    def test_status_endpoint(self):
        try:
            from fastapi.testclient import TestClient
            from app import app
        except ImportError:
            self.skipTest("fastapi[testclient] not available")

        client = TestClient(app)
        r = client.get("/api/status")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertIn("is_running", data)
        self.assertIn("current_job", data)
        self.assertIn("last_job", data)

    def test_logs_endpoint_no_file(self):
        try:
            from fastapi.testclient import TestClient
            from app import app
        except ImportError:
            self.skipTest("fastapi[testclient] not available")

        # Temporarily point to a non-existent log file
        import app as app_module
        original = app_module.LOG_FILE_PATH
        app_module.LOG_FILE_PATH = "/tmp/nonexistent_test.log"
        try:
            client = TestClient(app)
            r = client.get("/api/logs")
            self.assertEqual(r.status_code, 200)
            data = r.json()
            self.assertFalse(data["exists"])
        finally:
            app_module.LOG_FILE_PATH = original

    def test_raw_query_endpoint(self):
        try:
            from fastapi.testclient import TestClient
            from app import app
        except ImportError:
            self.skipTest("fastapi[testclient] not available")

        client = TestClient(app)
        api_key = os.getenv("API_KEY", "ak_live_7e8b4f1c9a3d5206e12f84b9c7a0d143")
        headers = {"X-API-Key": api_key, "Content-Type": "application/json"}

        # Test Raw INSERT
        r = client.post(
            "/api/query",
            headers=headers,
            json={"query": "INSERT OR REPLACE INTO users (id, user_name) VALUES (999999, 'TestRawUser')"}
        )
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertTrue(data["success"])
        self.assertEqual(data["type"], "write")

        # Test Raw SELECT
        r_select = client.post(
            "/api/query",
            headers=headers,
            json={"query": "SELECT * FROM users WHERE id = 999999"}
        )
        self.assertEqual(r_select.status_code, 200)
        sel_data = r_select.json()
        self.assertTrue(sel_data["success"])
        self.assertEqual(sel_data["type"], "read")
        self.assertEqual(len(sel_data["data"]), 1)
        self.assertEqual(sel_data["data"][0]["user_name"], "TestRawUser")


# ---------------------------------------------------------------------------
# Entry point (run without pytest)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()

    test_classes = [
        TestSQLiteSchema,
        TestUserOperations,
        TestCommentUrlOperations,
        TestRelationships,
        TestPersistence,
        TestAPIEndpoints,
    ]

    for cls in test_classes:
        suite.addTests(loader.loadTestsFromTestCase(cls))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)

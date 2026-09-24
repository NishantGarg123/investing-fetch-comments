import os
import time
import uuid
import threading
from typing import Optional, List, Dict, Any, Union
from datetime import datetime

from fastapi import FastAPI, HTTPException, BackgroundTasks, Query, status, Body, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from fetch_comment_urls import run_scraper, MAX_PAGES_PER_USER, DatabaseManager, DB_PATH
from aws_billing import router as billing_router, verify_api_key

# =====================================================================
# APPLICATION CONFIGURATION
# =====================================================================

app = FastAPI(
    title="Investing Comment Scraper API",
    description="REST API service to trigger and monitor Investing.com comment URL scraping jobs inside Docker.",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(billing_router)

APP_START_TIME = time.time()
LOG_FILE_PATH = os.getenv("LOG_FILE", "comment_scraper.log")

# Concurrency & Job State Management
_state_lock = threading.Lock()
_job_state = {
    "is_running": False,
    "current_job": None,
    "last_job": None,
    "job_history": []
}


# =====================================================================
# PYDANTIC SCHEMAS
# =====================================================================

class RunRequest(BaseModel):
    user_id: Optional[Union[int, str]] = Field(None, description="Single Investing.com user ID to scrape")
    id: Optional[Union[int, str]] = Field(None, description="Alias for user_id")
    userId: Optional[Union[int, str]] = Field(None, description="Alias for user_id")
    user_name: Optional[str] = Field(None, description="User name for single user execution")
    username: Optional[str] = Field(None, description="Alias for user_name")
    name: Optional[str] = Field(None, description="Alias for user_name")
    user: Optional[Union[Dict[str, Any], int, str]] = Field(None, description="User object, ID, or user name")
    users: Optional[Union[List[Union[Dict[str, Any], int, str]], Dict[str, Any], int, str]] = Field(None, description="List of user objects or IDs")
    pages: Optional[int] = Field(None, ge=1, le=100, description=f"Number of pages to scrape per user (default: {MAX_PAGES_PER_USER})")
    limit: Optional[int] = Field(None, ge=1, description="Maximum number of users to fetch from DB")
    dry_run: bool = Field(False, description="Run in dry-run mode without writing to database")
    proxy: Optional[Union[str, List[str]]] = Field(None, description="Proxy or list of proxies (e.g. username:password.218.218.252:5432)")
    proxies: Optional[Union[List[str], str]] = Field(None, description="Alias for proxy")
    proxy_file: Optional[str] = Field(None, description="Path to file containing list of proxies")
    background: bool = Field(True, description="Run asynchronously in background (returns immediately)")

    model_config = {
        "extra": "allow",
        "json_schema_extra": {
            "example": {
                "user_id": 274394243,
                "user_name": "Cash Patel",
                "pages": 1,
                "dry_run": False,
                "proxy": "username:password.218.218.252:5432",
                "background": True
            }
        }
    }


# =====================================================================
# BACKGROUND WORKER EXECUTION
# =====================================================================

def _execute_scraper_job(
    job_id: str,
    user_id: Optional[Union[int, str]],
    user_name: Optional[str],
    users: Optional[List[Dict[str, Any]]],
    pages: Optional[int],
    limit: Optional[int],
    dry_run: bool,
    proxy: Optional[Union[str, List[str]]] = None,
    proxy_file: Optional[str] = None
):
    def progress_update(current_idx: int, total: int, u_id: int, u_name: str):
        with _state_lock:
            if _job_state["current_job"] and _job_state["current_job"]["job_id"] == job_id:
                _job_state["current_job"]["progress"] = {
                    "current": current_idx,
                    "total": total,
                    "percentage": round((current_idx / total) * 100, 1) if total > 0 else 100,
                    "current_user_id": u_id,
                    "current_user_name": u_name
                }

    try:
        summary = run_scraper(
            user_id=user_id,
            user_name=user_name,
            users=users,
            pages=pages,
            limit=limit,
            dry_run=dry_run,
            proxy=proxy,
            proxy_file=proxy_file,
            progress_callback=progress_update
        )
    except Exception as e:
        summary = {
            "status": "failed",
            "error": str(e),
            "start_time": datetime.utcnow().isoformat(),
            "end_time": datetime.utcnow().isoformat(),
            "duration_seconds": 0
        }

    with _state_lock:
        finished_job = {
            "job_id": job_id,
            "params": {
                "user_id": user_id,
                "user_name": user_name,
                "users": users,
                "pages": pages,
                "limit": limit,
                "dry_run": dry_run,
                "proxy": proxy,
                "proxy_file": proxy_file
            },
            **summary
        }
        _job_state["last_job"] = finished_job
        _job_state["job_history"].append({
            "job_id": job_id,
            "status": summary.get("status"),
            "users_processed": summary.get("users_processed", 0),
            "comments_saved": summary.get("comments_saved", 0),
            "duration_seconds": summary.get("duration_seconds", 0),
            "end_time": summary.get("end_time")
        })
        # Keep last 50 history entries
        if len(_job_state["job_history"]) > 50:
            _job_state["job_history"] = _job_state["job_history"][-50:]

        _job_state["is_running"] = False
        _job_state["current_job"] = None


# =====================================================================
# REST ENDPOINTS
# =====================================================================

@app.get("/", summary="Root API Information")
def root():
    return {
        "service": "Investing Comment Scraper API",
        "status": "running",
        "uptime_seconds": round(time.time() - APP_START_TIME, 2),
        "docs_url": "/docs",
        "endpoints": {
            "trigger_run_post": "POST /api/run",
            "trigger_run_get": "GET /api/run",
            "check_status": "GET /api/status",
            "view_logs": "GET /api/logs",
            "health_check": "GET /health"
        }
    }


@app.get("/health", summary="Health Check")
def health_check():
    return {
        "status": "healthy",
        "timestamp": datetime.utcnow().isoformat(),
        "is_scraper_active": _job_state["is_running"]
    }


@app.post("/api/run", summary="Trigger Scraper Job (POST)")
def trigger_scraper_post(request: Optional[RunRequest] = Body(None)):
    if request is None:
        request = RunRequest()

    req_dict = request.model_dump(exclude_unset=False)
    if hasattr(request, '__pydantic_extra__') and request.__pydantic_extra__:
        req_dict.update(request.__pydantic_extra__)

    raw_user_id = req_dict.get('user_id') if req_dict.get('user_id') is not None else (
        req_dict.get('id') if req_dict.get('id') is not None else req_dict.get('userId')
    )
    raw_user_name = req_dict.get('user_name') or req_dict.get('username') or req_dict.get('name')
    raw_user = req_dict.get('user')
    raw_users = req_dict.get('users')

    if raw_user is not None:
        if isinstance(raw_user, dict):
            if raw_user_id is None:
                raw_user_id = raw_user.get('user_id') if raw_user.get('user_id') is not None else (
                    raw_user.get('id') if raw_user.get('id') is not None else raw_user.get('userId')
                )
            if not raw_user_name:
                raw_user_name = raw_user.get('user_name') or raw_user.get('username') or raw_user.get('name')
        elif isinstance(raw_user, int) or (isinstance(raw_user, str) and raw_user.strip().isdigit()):
            if raw_user_id is None:
                raw_user_id = raw_user
        elif isinstance(raw_user, str) and raw_user.strip():
            if not raw_user_name:
                raw_user_name = raw_user.strip()

    user_id_val = None
    if raw_user_id is not None and str(raw_user_id).strip():
        try:
            user_id_val = int(str(raw_user_id).strip())
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid user_id: '{raw_user_id}'. Must be an integer.")

    user_name_val = str(raw_user_name).strip() if (raw_user_name is not None and str(raw_user_name).strip()) else None

    users_val = None
    if raw_users is not None:
        if isinstance(raw_users, list):
            users_val = raw_users
        elif isinstance(raw_users, (dict, int, str)):
            users_val = [raw_users]

    raw_proxy = req_dict.get('proxy') if req_dict.get('proxy') is not None else req_dict.get('proxies')
    raw_proxy_file = req_dict.get('proxy_file') or req_dict.get('proxyFile') or req_dict.get('proxies_file')

    return _start_scraper_job(
        user_id=user_id_val,
        user_name=user_name_val,
        users=users_val,
        pages=request.pages,
        limit=request.limit,
        dry_run=request.dry_run,
        proxy=raw_proxy,
        proxy_file=raw_proxy_file,
        background=request.background
    )


@app.get("/api/run", summary="Trigger Scraper Job (GET convenience endpoint)")
def trigger_scraper_get(
    user_id: Optional[Union[int, str]] = Query(None, description="Specific user ID to scrape"),
    id: Optional[Union[int, str]] = Query(None, description="Alias for user_id"),
    userId: Optional[Union[int, str]] = Query(None, description="Alias for user_id"),
    user_name: Optional[str] = Query(None, description="User name for single user execution"),
    username: Optional[str] = Query(None, description="Alias for user_name"),
    name: Optional[str] = Query(None, description="Alias for user_name"),
    user: Optional[str] = Query(None, description="Alias for user_name or user_id"),
    pages: Optional[int] = Query(None, ge=1, le=100, description="Max pages per user"),
    limit: Optional[int] = Query(None, ge=1, description="Max users to fetch from DB"),
    dry_run: bool = Query(False, description="Run in dry-run mode"),
    proxy: Optional[str] = Query(None, description="Proxy or comma-separated list of proxies (e.g. username:password.218.218.252:5432)"),
    proxy_file: Optional[str] = Query(None, description="Path to file containing list of proxies"),
    background: bool = Query(True, description="Run in background")
):
    raw_user_id = user_id if user_id is not None else (id if id is not None else userId)
    raw_user_name = user_name or username or name

    if user is not None and str(user).strip():
        u_str = str(user).strip()
        if u_str.isdigit():
            if raw_user_id is None:
                raw_user_id = u_str
        else:
            if not raw_user_name:
                raw_user_name = u_str

    user_id_val = None
    if raw_user_id is not None and str(raw_user_id).strip():
        try:
            user_id_val = int(str(raw_user_id).strip())
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid user_id: '{raw_user_id}'. Must be an integer.")

    user_name_val = (raw_user_name or "").strip() or None

    return _start_scraper_job(
        user_id=user_id_val,
        user_name=user_name_val,
        users=None,
        pages=pages,
        limit=limit,
        dry_run=dry_run,
        proxy=proxy,
        proxy_file=proxy_file,
        background=background
    )


def _start_scraper_job(
    user_id: Optional[int] = None,
    user_name: Optional[str] = None,
    users: Optional[List[Dict[str, Any]]] = None,
    pages: Optional[int] = None,
    limit: Optional[int] = None,
    dry_run: bool = False,
    proxy: Optional[Union[str, List[str]]] = None,
    proxy_file: Optional[str] = None,
    background: bool = True
):
    with _state_lock:
        if _job_state["is_running"]:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "message": "A scraper job is already currently running.",
                    "current_job": _job_state["current_job"]
                }
            )

        job_id = f"job_{uuid.uuid4().hex[:8]}"
        _job_state["is_running"] = True
        _job_state["current_job"] = {
            "job_id": job_id,
            "started_at": datetime.utcnow().isoformat(),
            "params": {
                "user_id": user_id,
                "user_name": user_name,
                "users": users,
                "pages": pages,
                "limit": limit,
                "dry_run": dry_run,
                "proxy": proxy,
                "proxy_file": proxy_file
            },
            "progress": {
                "current": 0,
                "total": 0,
                "percentage": 0,
                "current_user_id": None,
                "current_user_name": ""
            }
        }

    if background:
        worker_thread = threading.Thread(
            target=_execute_scraper_job,
            args=(job_id, user_id, user_name, users, pages, limit, dry_run, proxy, proxy_file),
            daemon=True
        )
        worker_thread.start()

        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content={
                "message": "Scraper job started in background.",
                "job_id": job_id,
                "status_url": "/api/status",
                "logs_url": "/api/logs",
                "params": {
                    "user_id": user_id,
                    "user_name": user_name,
                    "users": users,
                    "pages": pages,
                    "limit": limit,
                    "dry_run": dry_run,
                    "proxy": proxy,
                    "proxy_file": proxy_file
                }
            }
        )
    else:
        # Synchronous execution
        _execute_scraper_job(job_id, user_id, user_name, users, pages, limit, dry_run, proxy, proxy_file)
        return _job_state["last_job"]


@app.get("/api/status", summary="Get Scraper Status & Progress")
def get_scraper_status():
    with _state_lock:
        return {
            "is_running": _job_state["is_running"],
            "current_job": _job_state["current_job"],
            "last_job": _job_state["last_job"],
            "recent_history": _job_state["job_history"][-10:]
        }


@app.get("/api/logs", summary="Get Recent Scraper Logs")
def get_scraper_logs(lines: int = Query(100, ge=1, le=1000, description="Number of recent log lines to fetch")):
    if not os.path.exists(LOG_FILE_PATH):
        return {
            "log_file": LOG_FILE_PATH,
            "exists": False,
            "lines": []
        }

    try:
        with open(LOG_FILE_PATH, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
            recent_lines = [line.rstrip() for line in all_lines[-lines:]]
            return {
                "log_file": LOG_FILE_PATH,
                "total_lines": len(all_lines),
                "returned_lines": len(recent_lines),
                "lines": recent_lines
            }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to read log file: {e}")


# =====================================================================
# FRONTEND / BACKEND DATA PERSISTENCE ENDPOINTS (SQLite)
# =====================================================================

@app.post(
    "/api/frontend-data",
    summary="Insert / Upsert Frontend & Backend Data into SQLite",
    dependencies=[Depends(verify_api_key)]
)
def save_frontend_data(
    payload: Union[Dict[str, Any], List[Dict[str, Any]]] = Body(...),
    api_key: str = Depends(verify_api_key)
):
    """
    Inserts or Upserts records into SQLite database.
    - If payload has 'id', it updates existing record or inserts if new (UPSERT).
    - If payload does not have 'id', a unique ID is auto-generated.
    - Accepts a single JSON object or a list of JSON objects.
    """
    db = DatabaseManager()
    try:
        if isinstance(payload, list):
            results = [db.upsert_frontend_data(item) for item in payload]
            return {
                "success": True,
                "message": f"Successfully saved {len(results)} records.",
                "count": len(results),
                "data": results
            }
        elif isinstance(payload, dict):
            res = db.upsert_frontend_data(payload)
            return {
                "success": True,
                "message": "Data saved successfully.",
                "data": res
            }
        else:
            raise HTTPException(status_code=400, detail="Invalid JSON payload. Expected object or array.")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {e}")
    finally:
        db.close()


@app.get(
    "/api/frontend-data",
    summary="Read Frontend / Backend Data from SQLite",
    dependencies=[Depends(verify_api_key)]
)
def get_frontend_data(
    limit: int = Query(50, ge=1, le=1000, description="Max number of records to return"),
    offset: int = Query(0, ge=0, description="Pagination offset"),
    id: Optional[str] = Query(None, description="Filter by unique ID"),
    user_id: Optional[int] = Query(None, description="Filter by user_id"),
    status: Optional[str] = Query(None, description="Filter by status"),
    api_key: str = Depends(verify_api_key)
):
    """
    Reads records from SQLite database with filtering, sorting, and pagination.
    """
    db = DatabaseManager()
    try:
        records = db.get_frontend_data(
            limit=limit,
            offset=offset,
            item_id=id,
            user_id=user_id,
            status=status
        )
        return {
            "success": True,
            "count": len(records),
            "limit": limit,
            "offset": offset,
            "data": records
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {e}")
    finally:
        db.close()


@app.get(
    "/api/frontend-data/{item_id}",
    summary="Get Single Record by ID from SQLite",
    dependencies=[Depends(verify_api_key)]
)
def get_frontend_data_by_id(item_id: str, api_key: str = Depends(verify_api_key)):
    """
    Reads a single record by its unique ID.
    """
    db = DatabaseManager()
    try:
        records = db.get_frontend_data(item_id=item_id, limit=1)
        if not records:
            raise HTTPException(status_code=404, detail=f"Record with ID '{item_id}' not found.")
        return {
            "success": True,
            "data": records[0]
        }
    finally:
        db.close()


@app.delete(
    "/api/frontend-data/{item_id}",
    summary="Delete Record by ID from SQLite",
    dependencies=[Depends(verify_api_key)]
)
def delete_frontend_data(item_id: str, api_key: str = Depends(verify_api_key)):
    """
    Deletes a record from SQLite by ID.
    """
    db = DatabaseManager()
    try:
        deleted = db.delete_frontend_data(item_id)
        if not deleted:
            raise HTTPException(status_code=404, detail=f"Record with ID '{item_id}' not found.")
        return {
            "success": True,
            "message": f"Record '{item_id}' deleted successfully."
        }
    finally:
        db.close()


# =====================================================================
# USER MANAGEMENT ENDPOINTS (SQLite)
# =====================================================================

class UserRequest(BaseModel):
    user_id: Optional[Union[int, str]] = Field(None, description="Investing.com User ID")
    id: Optional[Union[int, str]] = Field(None, description="Alias for user_id")
    userId: Optional[Union[int, str]] = Field(None, description="Alias for user_id")
    user_name: Optional[str] = Field(None, description="Investing.com username")
    username: Optional[str] = Field(None, description="Alias for user_name")
    name: Optional[str] = Field(None, description="Alias for user_name")


@app.post(
    "/api/users",
    summary="Add or Update User in SQLite Database",
    dependencies=[Depends(verify_api_key)]
)
def add_or_update_user(
    request: Union[UserRequest, Dict[str, Any], List[Dict[str, Any]]] = Body(...),
    api_key: str = Depends(verify_api_key)
):
    """
    Adds a new user or updates an existing user in SQLite.
    Body format:
      {
        "user_id": 200548123,
        "user_name": "InvestingUser"
      }
    """
    db = DatabaseManager()
    try:
        if isinstance(request, list):
            results = []
            for item in request:
                uid = item.get("user_id") or item.get("id") or item.get("userId")
                uname = item.get("user_name") or item.get("username") or item.get("name") or ""
                if uid is None:
                    continue
                results.append(db.upsert_user(int(uid), str(uname)))
            return {
                "success": True,
                "message": f"Successfully processed {len(results)} users.",
                "users": results
            }

        # Dict or UserRequest
        req_data = request.model_dump() if hasattr(request, "model_dump") else dict(request)
        uid = req_data.get("user_id") or req_data.get("id") or req_data.get("userId")
        uname = req_data.get("user_name") or req_data.get("username") or req_data.get("name") or ""

        if uid is None:
            raise HTTPException(status_code=400, detail="Missing required field: 'user_id' or 'id'.")

        try:
            uid_int = int(uid)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid user_id '{uid}'. Must be an integer.")

        saved_user = db.upsert_user(user_id=uid_int, user_name=str(uname))
        return {
            "success": True,
            "message": "User added/updated successfully.",
            "user": saved_user
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {e}")
    finally:
        db.close()


@app.get(
    "/api/users",
    summary="Get List of Users from SQLite Database",
    dependencies=[Depends(verify_api_key)]
)
def get_all_users(
    limit: Optional[int] = Query(None, ge=1, le=50000, description="Max number of users to return"),
    api_key: str = Depends(verify_api_key)
):
    """
    Returns list of all users from SQLite.
    """
    db = DatabaseManager()
    try:
        users = db.get_users(limit=limit)
        return {
            "success": True,
            "count": len(users),
            "users": users
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {e}")
    finally:
        db.close()


@app.get(
    "/api/users/{user_id}",
    summary="Get User by ID from SQLite Database",
    dependencies=[Depends(verify_api_key)]
)
def get_user_by_id(user_id: int, api_key: str = Depends(verify_api_key)):
    """
    Returns a single user by ID.
    """
    db = DatabaseManager()
    try:
        user_name = db.get_user_name(user_id=user_id)
        if not user_name:
            raise HTTPException(status_code=404, detail=f"User with ID {user_id} not found.")
        return {
            "success": True,
            "user": {
                "id": user_id,
                "user_name": user_name
            }
        }
    finally:
        db.close()


@app.delete(
    "/api/users/{user_id}",
    summary="Delete User by ID from SQLite Database",
    dependencies=[Depends(verify_api_key)]
)
def delete_user_by_id(user_id: int, api_key: str = Depends(verify_api_key)):
    """
    Deletes a user from SQLite by ID.
    """
    db = DatabaseManager()
    try:
        deleted = db.delete_user(user_id=user_id)
        if not deleted:
            raise HTTPException(status_code=404, detail=f"User with ID {user_id} not found.")
        return {
            "success": True,
            "message": f"User {user_id} deleted successfully."
        }
    finally:
        db.close()


# =====================================================================
# RAW SQL QUERY EXECUTION ENDPOINTS (Direct SQLite Access)
# =====================================================================

class QueryRequest(BaseModel):
    query: Optional[str] = Field(None, description="SQL query to execute (SELECT, INSERT, UPDATE, DELETE, etc.)")
    sql: Optional[str] = Field(None, description="Alias for query")
    params: Optional[Union[List[Any], Dict[str, Any]]] = Field(None, description="Optional parameter list or dict for parameterized queries")

    model_config = {
        "extra": "allow",
        "json_schema_extra": {
            "example": {
                "query": "SELECT * FROM users ORDER BY id DESC LIMIT 10",
                "params": []
            }
        }
    }


@app.post(
    "/api/query",
    summary="Execute Raw SQL Query Directly in SQLite",
    dependencies=[Depends(verify_api_key)],
    tags=["Database Direct Queries"]
)
@app.post(
    "/api/sql",
    summary="Execute Raw SQL Query Directly in SQLite (Alias)",
    dependencies=[Depends(verify_api_key)],
    tags=["Database Direct Queries"]
)
def execute_raw_sql(
    request: Union[QueryRequest, Dict[str, Any], str] = Body(...),
    api_key: str = Depends(verify_api_key)
):
    """
    Directly runs ANY SQL query against the SQLite database and returns the result.
    - Handles SELECT, PRAGMA, EXPLAIN (returns column headers, row counts, and data rows)
    - Handles INSERT, UPDATE, DELETE, CREATE, DROP (runs within write-locked transaction and returns rows_affected, last_insert_rowid)
    - Supports optional parameterized inputs via 'params'
    """
    raw_query = None
    params = None

    if isinstance(request, str):
        raw_query = request
    elif isinstance(request, dict):
        raw_query = request.get("query") or request.get("sql")
        params = request.get("params") or request.get("parameters")
    elif hasattr(request, "query"):
        raw_query = request.query or request.sql
        params = request.params

    if not raw_query or not str(raw_query).strip():
        raise HTTPException(status_code=400, detail="Missing required 'query' or 'sql' string in request body.")

    db = DatabaseManager()
    try:
        result = db.execute_query(str(raw_query).strip(), params=params)
        return {
            "success": True,
            "query": str(raw_query).strip(),
            **result
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"SQL Execution Error: {str(e)}")
    finally:
        db.close()


@app.get(
    "/api/query",
    summary="Execute Raw SQL Read Query via GET",
    dependencies=[Depends(verify_api_key)],
    tags=["Database Direct Queries"]
)
def execute_raw_sql_get(
    query: Optional[str] = Query(None, description="SQL query to execute"),
    sql: Optional[str] = Query(None, description="Alias for query"),
    api_key: str = Depends(verify_api_key)
):
    """
    Convenience GET endpoint to execute read queries (e.g. SELECT * FROM users) via URL.
    """
    target_query = query or sql
    if not target_query or not str(target_query).strip():
        raise HTTPException(status_code=400, detail="Missing required 'query' or 'sql' parameter.")

    db = DatabaseManager()
    try:
        result = db.execute_query(str(target_query).strip())
        return {
            "success": True,
            "query": str(target_query).strip(),
            **result
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"SQL Execution Error: {str(e)}")
    finally:
        db.close()


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", os.getenv("API_PORT", "3000")))
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=True)

import os
import time
import uuid
import threading
from typing import Optional, List, Dict, Any
from datetime import datetime

from fastapi import FastAPI, HTTPException, BackgroundTasks, Query, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from fetch_comment_urls import run_scraper, MAX_PAGES_PER_USER

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
    user_id: Optional[int] = Field(None, description="Single Investing.com user ID to scrape")
    pages: Optional[int] = Field(None, ge=1, le=100, description=f"Number of pages to scrape per user (default: {MAX_PAGES_PER_USER})")
    limit: Optional[int] = Field(None, ge=1, description="Maximum number of users to fetch from DB")
    dry_run: bool = Field(False, description="Run in dry-run mode without writing to database")
    background: bool = Field(True, description="Run asynchronously in background (returns immediately)")


# =====================================================================
# BACKGROUND WORKER EXECUTION
# =====================================================================

def _execute_scraper_job(
    job_id: str,
    user_id: Optional[int],
    pages: Optional[int],
    limit: Optional[int],
    dry_run: bool
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
            pages=pages,
            limit=limit,
            dry_run=dry_run,
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
                "pages": pages,
                "limit": limit,
                "dry_run": dry_run
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
def trigger_scraper_post(request: RunRequest):
    return _start_scraper_job(
        user_id=request.user_id,
        pages=request.pages,
        limit=request.limit,
        dry_run=request.dry_run,
        background=request.background
    )


@app.get("/api/run", summary="Trigger Scraper Job (GET convenience endpoint)")
def trigger_scraper_get(
    user_id: Optional[int] = Query(None, description="Specific user ID to scrape"),
    pages: Optional[int] = Query(None, ge=1, le=100, description="Max pages per user"),
    limit: Optional[int] = Query(None, ge=1, description="Max users to fetch from DB"),
    dry_run: bool = Query(False, description="Run in dry-run mode"),
    background: bool = Query(True, description="Run in background")
):
    return _start_scraper_job(
        user_id=user_id,
        pages=pages,
        limit=limit,
        dry_run=dry_run,
        background=background
    )


def _start_scraper_job(
    user_id: Optional[int],
    pages: Optional[int],
    limit: Optional[int],
    dry_run: bool,
    background: bool
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
                "pages": pages,
                "limit": limit,
                "dry_run": dry_run
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
            args=(job_id, user_id, pages, limit, dry_run),
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
                    "pages": pages,
                    "limit": limit,
                    "dry_run": dry_run
                }
            }
        )
    else:
        # Synchronous execution
        _execute_scraper_job(job_id, user_id, pages, limit, dry_run)
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


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", os.getenv("API_PORT", "3000")))
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=True)

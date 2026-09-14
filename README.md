# Investing.com Comment URLs Scraper & REST API

Containerized scraping service and REST API that extracts comment URLs and comment IDs for users from Investing.com and stores them into Microsoft SQL Server database.

---

## 🐳 Running with Docker (Recommended)

### 1. Configure Environment (`.env`)
Ensure your `.env` file contains your database credentials:
```ini
DB_DRIVER={ODBC Driver 17 for SQL Server}
# If SQL Server is on your host machine (Windows):
DB_SERVER=host.docker.internal
DB_NAME=InvestingDB
DB_USER=sa
DB_PASSWORD=your_password
DB_TRUSTED_CONNECTION=no
DB_ENCRYPT=no
DB_TRUST_CERT=yes

MAX_PAGES_PER_USER=2
```

### 2. Start the Container
Run with Docker Compose:
```bash
docker compose up -d --build
```
Or build and run directly with Docker:
```bash
docker build -t investing-scraper .
docker run -d -p 3000:3000 --env-file .env --name investing_comment_scraper investing-scraper
```

### 3. Check Container Status & Logs
```bash
docker compose ps
docker compose logs -f
```

---

## 🌐 REST API Usage

Once running, the interactive Swagger UI is available at:
👉 **`http://localhost:3000/docs`**

### API Endpoints Summary

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/api/run` | Triggers the scraping script (Accepts JSON body) |
| `GET` | `/api/run` | Triggers the scraping script via query params |
| `GET` | `/api/status` | Returns current scraper state, active progress, and last run stats |
| `GET` | `/api/logs` | Returns recent log output lines (`?lines=100`) |
| `GET` | `/health` | Health check endpoint |

---

### 📡 Trigger API Examples

#### 1. Trigger Full Scraper (Background Job)
```bash
curl -X POST http://localhost:3000/api/run \
     -H "Content-Type: application/json" \
     -d '{"dry_run": false, "background": true}'
```
**Response (202 Accepted):**
```json
{
  "message": "Scraper job started in background.",
  "job_id": "job_a1b2c3d4",
  "status_url": "/api/status",
  "logs_url": "/api/logs"
}
```

#### 2. Trigger Scraper for a Specific User
```bash
curl -X POST http://localhost:3000/api/run \
     -H "Content-Type: application/json" \
     -d '{"user_id": 201161838, "pages": 2, "dry_run": false}'
```

#### 3. Trigger Scraper via GET Request (Browser / Webhook)
```bash
curl "http://localhost:3000/api/run?user_id=201161838&pages=2"
```

#### 4. Trigger Scraper in Dry-Run Mode
```bash
curl -X POST http://localhost:3000/api/run \
     -H "Content-Type: application/json" \
     -d '{"limit": 5, "dry_run": true}'
```

#### 5. Check Scraper Status & Progress
```bash
curl http://localhost:3000/api/status
```
**Response:**
```json
{
  "is_running": true,
  "current_job": {
    "job_id": "job_a1b2c3d4",
    "started_at": "2026-09-11T10:30:00.000000",
    "progress": {
      "current": 3,
      "total": 10,
      "percentage": 30.0,
      "current_user_id": 201161838,
      "current_user_name": "JohnDoe"
    }
  },
  "last_job": null
}
```

#### 6. View Live Scraper Logs
```bash
curl "http://localhost:3000/api/logs?lines=50"
```

---

## 💻 Running Locally (Without Docker)

1. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

2. **Run the API server:**
   ```bash
   uvicorn app:app --host 0.0.0.0 --port 3000 --reload
   ```

3. **Or run the script directly via CLI:**
   ```bash
   # Run for all users in DB
   python fetch_comment_urls.py

   # Run for a specific user ID
   python fetch_comment_urls.py --user-id 201161838

   # Run dry-run mode
   python fetch_comment_urls.py --user-id 201161838 --dry-run
   ```

---

## 🛠️ Database Schema

### 1. Source Table (`InvestingDB.dbo.users`)
```sql
CREATE TABLE InvestingDB.dbo.users (
	id bigint NOT NULL,
	status nvarchar(20) COLLATE SQL_Latin1_General_CP1_CI_AS DEFAULT N'not processed' NOT NULL,
	user_name nvarchar(max) NULL,
	CONSTRAINT users_pkey PRIMARY KEY (id)
);
```

### 2. Destination Table (`InvestingDB.dbo.comment_urls`)
```sql
CREATE TABLE InvestingDB.dbo.comment_urls (
	id bigint NOT NULL,
	url nvarchar(2048) COLLATE SQL_Latin1_General_CP1_CI_AS NOT NULL,
	fetched_at datetime2 DEFAULT sysdatetime() NOT NULL,
	user_id bigint DEFAULT 0 NOT NULL,
	status nvarchar(50) COLLATE SQL_Latin1_General_CP1_CI_AS DEFAULT N'not processed' NOT NULL,
	Comments nvarchar(max) NULL,
	user_name nvarchar(max) NULL,
	CONSTRAINT comment_urls_pkey PRIMARY KEY (id),
	CONSTRAINT comment_urls_user_id_url_key UNIQUE (user_id,url)
);
CREATE NONCLUSTERED INDEX idx_comment_urls_user_id ON InvestingDB.dbo.comment_urls (user_id);
```

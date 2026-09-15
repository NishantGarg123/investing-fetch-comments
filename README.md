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

MAX_PAGES_PER_USER=1
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

#### 2. Trigger Scraper for a Specific User (Custom User Payload)
When user parameters are provided in the body, the scraper runs **only** for the specified user (bypassing `dbo.users` table querying):
```bash
curl -X POST http://localhost:3000/api/run \
     -H "Content-Type: application/json" \
     -d '{
       "user_name": "Cash Patel",
       "user_id": "274394243",
       "user": "Cash Patel",
       "pages": 1,
       "dry_run": false
     }'
```

#### 3. Trigger Scraper via GET Request (Browser / Webhook)
```bash
curl "http://localhost:3000/api/run?user_id=274394243&user_name=Cash%20Patel&pages=1"
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

## 🛡️ Proxy Configuration & Random Rotation

The scraper automatically selects a random proxy from your proxy list for each execution run to prevent IP rate-limiting and Cloudflare blocks.

### 1. Supported Proxy Formats
You can provide proxies in any of the following formats:
- `username:password.ip:port` *(e.g., `user123:pass456.218.218.252:5432`)*
- `username:password@ip:port` *(e.g., `user123:pass456@218.218.252:5432`)*
- `username:password:ip:port` *(e.g., `user123:pass456:218.218.252:5432`)*
- `ip:port:username:password` *(e.g., `218.218.252:5432:user123:pass456`)*
- `ip:port` *(e.g., `218.218.252:5432`)*
- `http://username:password@ip:port` / `socks5://...`

### 2. Providing Proxies

#### Method A: Proxy Text File (`proxies.txt`)
Create a file named `proxies.txt` (or refer to `proxies.txt.example`) in the project root with one proxy per line:
```text
username1:password1.218.218.252:5432
username2:password2.218.218.253:5432
```

#### Method B: Environment Variable (`.env`)
In your `.env` file:
```ini
PROXIES=username:password.218.218.252:5432,username2:password2.218.218.253:5432
# Or specify a custom file path:
# PROXY_FILE=proxies.txt
```

#### Method C: REST API Trigger Payload
Pass the `proxy` or `proxies` parameter in the `/api/run` request:
```bash
curl -X POST http://localhost:3000/api/run \
     -H "Content-Type: application/json" \
     -d '{
       "user_id": 274394243,
       "proxy": "username:password.218.218.252:5432",
       "background": true
     }'
```

#### Method D: CLI Execution
```bash
python fetch_comment_urls.py --proxy "username:password.218.218.252:5432"
# Or with a proxy file:
python fetch_comment_urls.py --proxy-file proxies.txt
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

# Investing.com Comment URLs Scraper

This script extracts comment URLs and comment IDs for users listed in the `InvestingDB.dbo.users` table from Investing.com (page 1 and page 2 by default) and stores them into the `InvestingDB.dbo.comment_urls` table using the `requests` library.

---

## 🛠️ Database Schema

### 1. Source Table (`InvestingDB.dbo.users`)
```sql
CREATE TABLE InvestingDB.dbo.users (
	id bigint NOT NULL,
	status nvarchar(20) COLLATE SQL_Latin1_General_CP1_CI_AS DEFAULT N'not processed' NOT NULL,
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

---

## 📦 Installation

1. Install Python dependencies:
   ```bash
   pip install -r requirements.txt
   ```

2. (Optional) Copy `.env.example` to `.env` and configure your database connection:
   ```bash
   copy .env.example .env
   ```

---

## 🚀 Usage

### 1. Run standard batch scraper (processes users from database status-independently):
```bash
python fetch_comment_urls.py
```

### 2. Run for a single user (e.g., User ID `201161838`):
```bash
python fetch_comment_urls.py --user-id 201161838
```

### 3. Dry-run mode (fetches and displays comments without writing to database):
```bash
python fetch_comment_urls.py --user-id 201161838 --dry-run
```

### 4. Custom max pages or limit:
```bash
python fetch_comment_urls.py --pages 2 --limit 50
```

---

## ⚙️ Features
- **Accurate Comment ID & URL Parsing**: Extracts unique comment IDs (`commentText-<id>` / `#<id>` / `comment=<id>`) and full URLs.
- **Status-Independent User Queue**: Reads users from the database without filtering by user status.
- **User Name from DB**: Resolves user name directly from the `users` table and saves it into `comment_urls`.
- **Default Comment URL Status**: Every saved comment URL is assigned `status = 'not processed'`.
- **Idempotent DB Inserts & Updates**: Skips duplicates automatically or updates user name/comments without duplicating records.
- **User Status Lifecycle**: Updates user status in `users` table to `'processed'`, `'no_comments'`, or `'failed'`.
- **Browser Emulation**: Uses realistic browser headers and retry strategy to prevent blocking.
- **Polite Delay**: Includes configurable randomized delays between requests.

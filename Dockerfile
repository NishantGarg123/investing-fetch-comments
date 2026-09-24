FROM python:3.11-slim

# Environment settings
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive

WORKDIR /app

# Install minimal system utilities (SQLite3 is bundled with Python 3.11-slim)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY . .

# Create the persistent data directory for SQLite
# This directory is mounted as a Docker volume so the .db file survives restarts/rebuilds
RUN mkdir -p /app/data

# Declare /app/data as a Docker volume mount point.
# In docker-compose.yml (or docker run -v) map this to a host/named volume.
VOLUME ["/app/data"]

# Expose API port
EXPOSE 3000

# Run FastAPI server
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "3000"]

FROM python:3.11-slim

# Prevent python from writing pyc files to disk and buffering stdout/stderr
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    sqlite3 \
    && rm -rf /var/lib/apt/lists/*

# Install python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN pip install --no-cache-dir uvicorn psutil gunicorn

# Copy application codebase
COPY . .

# Create non-root user for security
RUN useradd -m -u 1000 appuser && \
    mkdir -p /app/data && \
    chown -R appuser:appuser /app

# Switch to non-root user
USER appuser

# Create volume mount point for persistent database backups and data
VOLUME ["/app/data"]
ENV DATABASE_PATH=/app/data/trading_bot.db

EXPOSE 8000

# Default entrypoint starts the dashboard web service
CMD ["uvicorn", "src.dashboard.app:create_dashboard_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]

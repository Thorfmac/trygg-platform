FROM python:3.12-slim

WORKDIR /app

# Install PostgreSQL client (needed for pg_dump in backup script)
RUN apt-get update && apt-get install -y postgresql-client && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy all application code
COPY . .

# Default command — the scheduler
# The telegram service overrides this in docker-compose.yml
CMD ["python", "scheduler.py"]
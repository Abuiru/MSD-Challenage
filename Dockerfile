# Proactive Storage Health Monitor
FROM python:3.12-slim

WORKDIR /app

# Install dependencies first for better layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code, default config, and sample input
COPY monitor.py .
COPY config.yaml .
COPY storage_api_mock.json .

# Run as a non-root user
RUN useradd -m monitor && chown -R monitor:monitor /app
USER monitor

# Defaults can be overridden with --env / -v at `docker run` time:
#   -e MONITOR_CAPACITY_WARNING_PERCENT=80
#   -v /path/to/real_payload.json:/app/storage_api_mock.json:ro
ENTRYPOINT ["python", "monitor.py"]
CMD ["--input", "storage_api_mock.json", "--config", "config.yaml"]

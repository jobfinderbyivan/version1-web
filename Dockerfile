# Job Search Automation System — production image (Railway / any container host)
FROM python:3.12-slim

# System deps: pdf/docx parsing wheels are self-contained; we just need a
# clean build env. tzdata so the scheduler uses correct local time.
RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Bind to all interfaces inside the container; Railway injects $PORT.
ENV HOST=0.0.0.0 \
    PYTHONUNBUFFERED=1

# Single process on purpose: the in-app scheduler must not run in parallel
# workers (it would double-send scheduled emails). One uvicorn process handles
# the spec's 100-user target comfortably.
CMD ["python", "run.py"]

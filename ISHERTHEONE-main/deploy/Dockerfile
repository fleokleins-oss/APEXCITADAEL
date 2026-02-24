FROM python:3.12-slim

WORKDIR /app

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*

# Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code
COPY . .

# Default: maestro_v3 (override in docker-compose)
CMD ["uvicorn", "maestro_v3:app", "--host", "0.0.0.0", "--port", "8007"]

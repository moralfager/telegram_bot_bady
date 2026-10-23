FROM python:3.11-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Copy source code and assets
COPY run.py pyproject.toml ./
COPY src/ ./src/
COPY data/media/ ./data/media/

# Create persistent data volume directory
RUN mkdir -p /app/data

VOLUME ["/app/data"]

CMD ["python", "-X", "utf8", "-u", "run.py", "bots"]

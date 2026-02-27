FROM python:3.12-slim

RUN apt-get update && apt-get install -y \
    libopus0 \
    git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml .
RUN pip install --no-cache-dir .[server]

COPY . .

RUN mkdir -p /data

EXPOSE 8000

CMD ["python", "server.py"]

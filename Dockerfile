FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    wget curl \
    && rm -rf /var/lib/apt/lists/*

# Install litestream
RUN wget -qO /tmp/litestream.tar.gz \
    https://github.com/benbjohnson/litestream/releases/download/v0.3.13/litestream-v0.3.13-linux-amd64.tar.gz \
    && tar -xz -C /usr/local/bin -f /tmp/litestream.tar.gz \
    && rm /tmp/litestream.tar.gz

COPY pyproject.toml ./
RUN pip install --no-cache-dir -e .

COPY schema.sql ./
COPY migrations/ ./migrations/
COPY app/ ./app/
COPY run.sh ./

VOLUME ["/data"]

CMD ["/bin/sh", "run.sh"]

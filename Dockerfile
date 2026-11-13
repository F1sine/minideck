# Minideck management server (no GUI). Build and run:
#   docker build -t minideck-server .
#   docker run -d --name minideck -p 7072:7072 -v minideck-data:/data \
#       -e MINIDECK_PUBLIC=server.example.com minideck-server
# First admin password: docker logs minideck   (also in /data/ADMIN-PASSWORD.txt)
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MINIDECK_DATA=/data \
    MINIDECK_PORT=7072 \
    MINIDECK_PUBLIC=""

RUN pip install --no-cache-dir "cryptography>=42" \
 && useradd --system --uid 10001 --home-dir /data minideck \
 && mkdir /data && chown minideck /data

WORKDIR /app
COPY minideck/ /app/minideck/
COPY run_server.py deploy/docker-entrypoint.sh /app/
RUN chmod 755 /app/docker-entrypoint.sh

USER minideck
VOLUME ["/data"]
EXPOSE 7072
ENTRYPOINT ["/app/docker-entrypoint.sh"]

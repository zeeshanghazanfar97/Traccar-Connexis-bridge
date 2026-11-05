FROM python:3.12-alpine

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN addgroup -S -g 10001 adapter \
    && adduser -S -D -H -u 10001 -G adapter adapter \
    && mkdir -p /data \
    && chown adapter:adapter /data

WORKDIR /app
COPY adapter/ /app/adapter/
USER 10001:10001
ENTRYPOINT ["python", "-m", "adapter"]

# weatherbot tree12-allno — paper/observe runtime
#
# The observer runtime uses only the Python standard library (urllib, sqlite3,
# zoneinfo, decimal). eth-account in requirements.txt is only required by the
# offline order-signing test fixtures and is intentionally not installed here.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=UTC

WORKDIR /app

COPY . .

RUN mkdir -p /app/data

# The observer writes data/state.json on every deterministic stage and at
# startup. A missing file means the process never reached a usable state.
HEALTHCHECK --interval=120s --timeout=10s --start-period=120s --retries=3 \
    CMD test -f /app/data/state.json || exit 1

CMD ["python3", "metar_observer.py", "run", "--config", "/app/config.json"]

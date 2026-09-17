FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    V2_MODE=shadow \
    V2_LIVE_TRADING=false \
    V2_PERSISTENT_STATE=false \
    V2_STATE_BACKEND=sqlite \
    V2_PRIVATE_ADAPTER_ENABLED=false \
    V2_LIVE_AUTHORIZED=false \
    V2_LIVE_ENGINE_UNLOCK=false \
    V2_EMERGENCY_FLATTEN_VERIFIED=false \
    V2_STATE_DB=/data/v2_state.sqlite3

WORKDIR /app

COPY v2_bot/requirements.txt /app/v2_bot/requirements.txt
RUN pip install --no-cache-dir -r /app/v2_bot/requirements.txt \
    && useradd --create-home --uid 10001 v2bot \
    && mkdir -p /data \
    && chown -R v2bot:v2bot /data /app

COPY --chown=v2bot:v2bot v2_bot /app/v2_bot

USER v2bot

VOLUME ["/data"]

CMD ["python", "-m", "v2_bot.main"]

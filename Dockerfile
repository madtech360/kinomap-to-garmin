FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    GARMIN_TOKENS=/data/garminconnect \
    STATE_FILE=/data/state.json \
    LOG_FILE=/data/logs/k2g.log

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
 && useradd --create-home --uid 1000 app \
 && mkdir /data && chown app:app /data

COPY kinomap_to_garmin.py trigger_server.py entrypoint.sh ./
RUN chmod +x entrypoint.sh

USER app
VOLUME /data
EXPOSE 8765
ENTRYPOINT ["./entrypoint.sh"]

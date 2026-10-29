FROM python:3.12-slim

# ffmpeg 用于 --thumbs 抽海报；ps 里没有它也只是海报生成为空，不影响主链路
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY alembic.ini ./
COPY manju ./manju
COPY migrations ./migrations
COPY tests ./tests
COPY pytest.ini ./
COPY entrypoint.sh ./
RUN chmod +x entrypoint.sh

ENV PYTHONUNBUFFERED=1
EXPOSE 8000
ENTRYPOINT ["./entrypoint.sh"]

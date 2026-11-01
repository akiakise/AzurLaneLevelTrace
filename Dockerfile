FROM python:3.12-slim-bookworm

WORKDIR /app
COPY app.py /app/app.py
COPY blhx /app/blhx
COPY VERSION /app/VERSION

ENV DATA_DIR=/data \
    HOST=0.0.0.0 \
    PORT=8000 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/api/filter-options' % os.environ.get('PORT', '8000'), timeout=3)"

STOPSIGNAL SIGINT
CMD ["python", "app.py"]

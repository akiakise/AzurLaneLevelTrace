FROM python:3.12-slim

WORKDIR /app
COPY app.py /app/app.py

ENV DATA_DIR=/data \
    HOST=0.0.0.0 \
    PORT=8000 \
    PYTHONUNBUFFERED=1

VOLUME ["/data"]
EXPOSE 8000
CMD ["python", "app.py"]

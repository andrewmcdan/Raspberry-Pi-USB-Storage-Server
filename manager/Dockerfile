FROM python:3.12-slim
WORKDIR /app
COPY manager/requirements.txt manager/requirements.txt
RUN pip install --no-cache-dir -r manager/requirements.txt && useradd --uid 10001 --create-home manager && mkdir -p /data/content && chown manager:manager /data/content
COPY manager manager
COPY opt/piusb opt/piusb
COPY etc/piusb etc/piusb
COPY tests tests
USER manager
EXPOSE 8000
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "2", "--threads", "8", "--timeout", "3600", "manager.app:create_app()"]

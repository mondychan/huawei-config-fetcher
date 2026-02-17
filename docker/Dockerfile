FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app
COPY pyproject.toml README.md /app/
COPY src/ /app/src/

RUN pip install --no-cache-dir -U pip \
    && pip install --no-cache-dir ".[web]"

WORKDIR /data
ENV HCF_CONFIG_PATH=/data/config.toml
ENV HCF_WEB_HOST=0.0.0.0
ENV HCF_WEB_PORT=8080

EXPOSE 8080
CMD ["python", "-m", "huawei_config_fetcher.web"]

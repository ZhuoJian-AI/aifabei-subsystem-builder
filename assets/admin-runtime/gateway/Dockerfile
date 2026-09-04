FROM python:3.12-slim-bookworm

ARG PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /opt/zhuojian-storage-gateway

COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m pip install --no-cache-dir --index-url "$PIP_INDEX_URL" .

EXPOSE 8080

CMD ["uvicorn", "zhuojian_storage_gateway.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]

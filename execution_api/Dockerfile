FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml ./
COPY execution_api/ execution_api/

RUN pip install --no-cache-dir \
    fastapi>=0.115 uvicorn>=0.34 aiohttp>=3.9 httpx>=0.27 \
    pydantic>=2.0 prometheus_client>=0.20

EXPOSE 8000

ENV GATEWAY_URL=http://host.docker.internal:8888

CMD ["uvicorn", "execution_api.server:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]

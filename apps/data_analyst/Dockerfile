FROM python:3.12-slim

WORKDIR /app

COPY apps/data_analyst/ apps/data_analyst/

RUN pip install --no-cache-dir \
    chainlit>=2.0 anthropic>=0.40 openai>=1.0 httpx>=0.27

EXPOSE 8501

ENV EXECUTION_API_URL=http://host.docker.internal:8000
ENV LLM_PROVIDER=anthropic
ENV LLM_MODEL=claude-sonnet-4-20250514

CMD ["chainlit", "run", "apps/data_analyst/app.py", "--host", "0.0.0.0", "--port", "8501"]

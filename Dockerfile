FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install .

COPY research ./research

# Runtime state lives in mounted volumes: /app/logs, /app/data,
# /app/research/paper_trials (account resume + reports survive the container).
CMD ["python", "-m", "vnedge.runtime.paper_trial", \
     "research/paper_trials/funding_mr_btc_v1_20260703.yaml", \
     "--hours", "720", "--dashboard"]

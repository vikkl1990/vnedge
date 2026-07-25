FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install .

COPY research ./research
# docs/ ships too: the dashboard's /runbooks route serves docs/RUNBOOKS.md at
# runtime (without this the route 404s in-container — caught 2026-07-11).
COPY docs ./docs

# Build provenance: deploy.sh passes the deployed git sha as a build-arg. Kept
# LAST so changing it never invalidates the pip / COPY layers above.
ARG VNEDGE_BUILD_SHA=dev
RUN echo "$VNEDGE_BUILD_SHA" > /app/BUILD_SHA

# Runtime state lives in mounted volumes: /app/logs, /app/data,
# /app/research/paper_trials (account resume + reports survive the container).
CMD ["python", "-m", "vnedge.runtime.paper_trial", \
     "research/paper_trials/funding_mr_btc_v1_20260703.yaml", \
     "--hours", "720", "--dashboard"]

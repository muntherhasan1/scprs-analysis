# SCPRS analysis pipeline — portable image for scheduled cloud runs.
# Build:  docker build -t scprs-analysis .
# Run:    docker run --rm -v scprs-data:/app/data scprs-analysis src.warehouse info
#         docker run --rm -v scprs-data:/app/data scprs-analysis \
#           src.model enrich 8660 07/01/2021 06/30/2028 --limit 200 --newest-first
#
# The pipeline writes SQLite files under /app/data — mount a volume (as above) so
# they persist between runs. In a real cloud deployment, point the warehouse at a
# managed Postgres and use object storage for the raw extracts instead.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_NO_SANDBOX=1


WORKDIR /app

# Install Python deps and the Chromium browser (+ its OS libraries) that Playwright
# needs. `--with-deps` pulls the exact system packages for the installed Playwright.
COPY requirements.txt .
RUN pip install -r requirements.txt \
    && playwright install --with-deps chromium

# Application code + committed reference data (department list).
COPY src/ ./src/
COPY references/ ./references/
RUN mkdir -p data

# Run as a non-root user (defence in depth).
RUN useradd --create-home appuser && chown -R appuser /app
USER appuser

# `docker run <image> <module> <args...>` -> `python -m <module> <args...>`
ENTRYPOINT ["python", "-m"]
CMD ["src.warehouse", "info"]

"""Entrypoint for the SCPRS natural-language web app (Gradio).

Used both by the Docker image (`CMD python app.py`) and by Hugging Face Spaces,
which run the app file as ``__main__``. Bind host/port come from the
``GRADIO_SERVER_NAME`` / ``GRADIO_SERVER_PORT`` env vars (set in Dockerfile.web).
"""

import os

from src import data_sync, observability, warehouse_query
from src.web_app import build_demo

observability.init_sentry("web")  # optional error tracking; no-op unless SENTRY_DSN is set
# On a Space, fetch the slim serve DB from the private dataset before serving
# (no-op locally, where WAREHOUSE_DATASET is unset and the local DB is used).
data_sync.ensure_local_db(warehouse_query.WAREHOUSE_DB)
demo = build_demo()
# Cap concurrent work + queue depth so a burst of requests (or a bot) can't
# exhaust the shared Space or the Gemini free-tier quota. Pairs with the query
# time limit in warehouse_query. Env-tunable.
demo.queue(
    default_concurrency_limit=int(os.environ.get("GRADIO_CONCURRENCY", "4")),
    max_size=int(os.environ.get("GRADIO_QUEUE_MAX", "32")),
)

if __name__ == "__main__":
    demo.launch()

"""Entrypoint for the SCPRS natural-language web app (Gradio).

Used both by the Docker image (`CMD python app.py`) and by Hugging Face Spaces,
which run the app file as ``__main__``. Bind host/port come from the
``GRADIO_SERVER_NAME`` / ``GRADIO_SERVER_PORT`` env vars (set in Dockerfile.web).
"""

from src.web_app import build_demo

demo = build_demo()

if __name__ == "__main__":
    demo.launch()

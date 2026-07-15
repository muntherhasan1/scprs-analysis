"""Optional error tracking for the deployed front ends.

A no-op unless ``SENTRY_DSN`` is set, so local dev and tests are unaffected. Enable
by adding the ``SENTRY_DSN`` secret to a Space; unhandled exceptions in the MCP
server / web app are then captured with stack traces. No PII, no performance
tracing by default (this is a public, read-only service).
"""

from __future__ import annotations

import os


def init_sentry(component: str) -> bool:
    """Initialize Sentry if ``SENTRY_DSN`` is set. Returns True if enabled. Never
    raises — error tracking must not break startup."""
    dsn = os.environ.get("SENTRY_DSN")
    if not dsn:
        print(f"[observability] Sentry disabled (SENTRY_DSN not set) for component={component}")
        return False
    try:
        import sentry_sdk

        env = os.environ.get("SENTRY_ENV", "production")
        sentry_sdk.init(
            dsn=dsn,
            traces_sample_rate=0.0,
            send_default_pii=False,
            environment=env,
        )
        sentry_sdk.set_tag("component", component)
        print(f"[observability] Sentry enabled for component={component} env={env}")
        return True
    except Exception as exc:  # noqa: BLE001  # telemetry must not break startup
        print(f"[observability] Sentry init failed ({type(exc).__name__}: {exc}); continuing")
        return False

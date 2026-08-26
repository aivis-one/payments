"""ASGI entrypoint.

Bare application on purpose: routes are H3. There is no /health endpoint -- it
is not in the API contract, and the Dockerfile only needs a valid ASGI object.
"""

from __future__ import annotations

from fastapi import FastAPI

app = FastAPI(title="payments", version="0.1.0")

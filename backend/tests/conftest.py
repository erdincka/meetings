"""Shared test configuration.

Environment is set before any application import so that
``app.core.config.Settings`` -- which reads the environment once at import --
sees deterministic values rather than whatever the developer has exported.
"""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@127.0.0.1:5432/test")
os.environ.setdefault("INFERENCE_ENDPOINT", "http://inference.test/v1")
os.environ.setdefault("INFERENCE_API_KEY", "test-key")
os.environ.setdefault("INFERENCE_MODEL_NAME", "test-model")
os.environ.setdefault("EMBEDDING_ENDPOINT", "http://embedding.test/v1")
os.environ.setdefault("EMBEDDING_API_KEY", "test-key")
os.environ.setdefault("EMBEDDING_MODEL_NAME", "test-embed")
os.environ.setdefault("ALLOWED_ORIGINS", "http://localhost:3000")

"""Model settings for the independently packaged optimizer and batch runner."""

import os


def resolve_model(variable: str, model: str | None = None) -> str:
    value = (model if model is not None else os.getenv(variable, "")).strip()
    if not value:
        raise ValueError(f"Set {variable} in .env/environment or pass an explicit model")
    return value

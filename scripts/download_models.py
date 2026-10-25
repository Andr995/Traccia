"""Fetch compact model assets during a Render build, not on first user request."""

import os

from fastembed import TextEmbedding


model = TextEmbedding(
    model_name=os.environ.get(
        "EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2"
    ),
    cache_dir=os.environ.get("FASTEMBED_CACHE_PATH", ".model-cache"),
    threads=1,
    lazy_load=False,
)
# Force one short encode so the model files are fully initialized and available
# to the runtime image. The resulting vector is intentionally discarded.
next(iter(model.embed(["Traccia model warmup"])))
del model

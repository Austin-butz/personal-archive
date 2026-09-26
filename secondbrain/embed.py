"""Embedding backends. Default: a local Ollama server running nomic-embed-text (768-d)."""
import json
import os
import urllib.request

DIM = 768  # must match chunks_vec in schema.sql


class OllamaEmbedder:
    def __init__(self, model=None, url=None):
        self.model = model or os.environ.get("SECONDBRAIN_EMBED_MODEL", "nomic-embed-text")
        self.url = url or os.environ.get("OLLAMA_URL", "http://localhost:11434")
        self.name = f"ollama:{self.model}"

    def _embed(self, texts):
        req = urllib.request.Request(
            f"{self.url}/api/embed",
            data=json.dumps({"model": self.model, "input": texts}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=300) as r:
            vecs = json.load(r)["embeddings"]
        if vecs and len(vecs[0]) != DIM:
            raise ValueError(f"{self.name} returned {len(vecs[0])} dims, schema expects {DIM}")
        return vecs

    # nomic-embed-text expects task prefixes
    def documents(self, texts):
        return self._embed([f"search_document: {t}" for t in texts])

    def query(self, text):
        return self._embed([f"search_query: {text}"])[0]

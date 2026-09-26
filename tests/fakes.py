import hashlib

from secondbrain.embed import DIM


class FakeEmbedder:
    """Deterministic bag-of-words vectors, so similar text lands nearby."""
    name = "fake"

    def _vec(self, text):
        v = [0.0] * DIM
        for w in text.lower().split():
            v[int(hashlib.md5(w.encode()).hexdigest(), 16) % DIM] += 1.0
        n = sum(x * x for x in v) ** 0.5 or 1.0
        return [x / n for x in v]

    def documents(self, texts):
        return [self._vec(t) for t in texts]

    def query(self, text):
        return self._vec(text)

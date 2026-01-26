import numpy as np
from sentence_transformers import SentenceTransformer

_model = SentenceTransformer("all-MiniLM-L6-v2")

def embed(text: str) -> bytes:
    vec = _model.encode(text)
    return vec.astype("float32").tobytes()

def similarity(a: bytes, b: bytes) -> float:
    va = np.frombuffer(a, dtype="float32")
    vb = np.frombuffer(b, dtype="float32")
    return float(np.dot(va, vb) / (np.linalg.norm(va) * np.linalg.norm(vb)))

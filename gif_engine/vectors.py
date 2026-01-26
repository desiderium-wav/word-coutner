import numpy as np
from sentence_transformers import SentenceTransformer
from .config import EMBEDDING_DIM

_model = SentenceTransformer("all-MiniLM-L6-v2")

def embed(text: str) -> bytes:
    """
    Returns a float32 byte representation of the embedding with length EMBEDDING_DIM.
    Ensures consistent size by trimming or padding if necessary.
    """
    vec = _model.encode(text)
    arr = np.asarray(vec, dtype="float32")
    if arr.size != EMBEDDING_DIM:
        # normalize size: trim or pad with zeros
        if arr.size > EMBEDDING_DIM:
            arr = arr[:EMBEDDING_DIM]
        else:
            arr = np.pad(arr, (0, EMBEDDING_DIM - arr.size), constant_values=0.0)
    return arr.tobytes()

def similarity(a: bytes, b: bytes) -> float:
    va = np.frombuffer(a, dtype="float32")
    vb = np.frombuffer(b, dtype="float32")
    # Ensure same size
    if va.size != vb.size:
        minlen = min(va.size, vb.size)
        va = va[:minlen]
        vb = vb[:minlen]
    denom = (np.linalg.norm(va) * np.linalg.norm(vb))
    if denom == 0:
        return 0.0
    return float(np.dot(va, vb) / denom)

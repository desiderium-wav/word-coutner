import numpy as np
import logging
from sentence_transformers import SentenceTransformer
from .config import EMBEDDING_DIM

logger = logging.getLogger(__name__)

# Load model once
_model = SentenceTransformer("all-MiniLM-L6-v2")

def _normalize_array(arr: np.ndarray) -> np.ndarray:
    norm = np.linalg.norm(arr)
    if norm == 0:
        return arr
    return arr / norm

def embed(text: str) -> bytes:
    """
    Compute an embedding and return a L2-normalized float32 byte buffer of length EMBEDDING_DIM.
    If the model returns a different size, we'll trim or pad with zeros before normalization.
    """
    vec = _model.encode(text)
    arr = np.asarray(vec, dtype="float32")
    if arr.size != EMBEDDING_DIM:
        if arr.size > EMBEDDING_DIM:
            arr = arr[:EMBEDDING_DIM]
        else:
            arr = np.pad(arr, (0, EMBEDDING_DIM - arr.size), constant_values=0.0)
    # L2-normalize for fast cosine similarity as dot product
    arr = _normalize_array(arr)
    return arr.tobytes()

def similarity(a: bytes, b: bytes) -> float:
    """
    Compute cosine similarity. If embeddings are already normalized this is a dot product.
    To be robust, we will normalize if norms are far from 1.0 or sizes mismatch.
    """
    va = np.frombuffer(a, dtype="float32")
    vb = np.frombuffer(b, dtype="float32")

    # Ensure same size
    if va.size != vb.size:
        minlen = min(va.size, vb.size)
        va = va[:minlen]
        vb = vb[:minlen]

    # Quick path: if both norms are ~1, dot product is cosine
    na = np.linalg.norm(va)
    nb = np.linalg.norm(vb)
    if na == 0 or nb == 0:
        return 0.0

    # If norms differ noticeably from 1.0, normalize
    if not (0.99 <= na <= 1.01 and 0.99 <= nb <= 1.01):
        va = va / na
        vb = vb / nb
        return float(np.dot(va, vb))
    return float(np.dot(va, vb))

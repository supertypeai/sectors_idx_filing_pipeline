from __future__ import annotations
import re
from typing import Tuple
from rapidfuzz import fuzz

IDX_KNOWN = [
    "Ownership Report or Any Changes in Ownership of Public Company Shares",
    "REPORT OF OWNERSHIP OR ANY CHANGES IN SHARE OWNERSHIP OF PUBLIC COMPANIES"
]
NON_IDX_KNOWN = [
    "Share Ownership Report",
    "Laporan Kepemilikan Saham",
]

IDX_KNOWN_L = [s.lower() for s in IDX_KNOWN]
NON_IDX_KNOWN_L = [s.lower() for s in NON_IDX_KNOWN]

def _norm(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()
    return re.sub(r"\s+", " ", s)

def token_jaccard(a: str, b: str) -> float:
    A = set(_norm(a).split())
    B = set(_norm(b).split())
    if not A or not B:
        return 0.0
    return len(A & B) / len(A | B)

def low_title_similarity(title: str, filename: str, threshold: float = 0.35) -> tuple[bool, float]:
    base = re.sub(r"\.[a-z0-9]+$", "", filename.lower())
    score = token_jaccard(title or "", base or "")
    return (score < threshold, score)

def classify_format(title: str, threshold: int = 80) -> Tuple[str, int, int, int]:
    """Return (label, best_score, idx_score, non_idx_score)."""
    t = (title or "").strip().lower()
    idx_score = max((fuzz.token_set_ratio(t, k) for k in IDX_KNOWN_L), default=0)
    non_score = max((fuzz.token_set_ratio(t, k) for k in NON_IDX_KNOWN_L), default=0)
    best = max(idx_score, non_score)
    if best < threshold:
        return "UNKNOWN", best, idx_score, non_score
    return ("IDX" if idx_score >= non_score else "NON-IDX"), best, idx_score, non_score
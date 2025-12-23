from __future__ import annotations
from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional
from datetime import datetime, timezone

def as_list(x) -> List[Any]:
    if x is None:
        return []
    if isinstance(x, list):
        return x
    return [x]

def coerce_timestamp_iso(ts: Any) -> str:
    if isinstance(ts, str) and ts:
        return ts
    return datetime.now(timezone.utc).isoformat().replace("+00:00","Z")

@dataclass
class Article:
    title: str
    body: str
    source: str
    timestamp: Any
    company_name: str = ""
    # symbol: Optional[str] = None
    tickers: Optional[List[str]] = None
    sector: str = ""
    sub_sector: Optional[List[str]] = None
    tags: Optional[List[str]] = None
    sentiment: str = "neutral"
    dimension: Optional[Dict[str, Any]] = None
    score: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["sub_sector"] = as_list(d.get("sub_sector"))
        d["tickers"] = as_list(d.get("tickers"))
        d["tags"] = as_list(d.get("tags"))
        return d

# src/generate/filings/utils/consolidators.py
from __future__ import annotations
from typing import Dict, List, Tuple
from datetime import datetime

# Import the new standard type
from src.core.types import FilingRecord

def _key(record: FilingRecord) -> Tuple:
    """
    Generate a grouping key for a FilingRecord.
    Groups by key facts and date (DoD).
    """
    # We can trust the timestamp is already a clean ISO string
    d = ""
    if record.timestamp:
        try:
            d = datetime.fromisoformat(record.timestamp).date().isoformat()
        except Exception:
            pass
            
    return (
        record.symbol,
        record.holder_name,
        record.holding_before,
        record.holding_after,
        record.share_percentage_before,
        record.share_percentage_after,
        record.source,
        record.transaction_type,
        d,
    )

def dedupe_rows(records: List[FilingRecord]) -> List[FilingRecord]:
    """
    Deduplicates a list of FilingRecord objects based on the _key.
    If duplicates are found, it keeps the one that seems 'edited'
    or has the earliest 'created_at' timestamp (from raw_data).
    """
    groups: Dict[Tuple, List[FilingRecord]] = {}
    for r in records:
        groups.setdefault(_key(r), []).append(r)

    out: List[FilingRecord] = []
    for k, grp in groups.items():
        if len(grp) == 1:
            out.append(grp[0])
            continue
            
        # Keep earliest created / or one already edited (heuristic)
        # We access raw_data for fields that aren't on the core record
        def score(x: FilingRecord):
            raw = x.raw_data
            edited = 1 if raw.get("edited_by") or raw.get("review_notes") else 0
            created = raw.get("created_at") or ""
            # Sort by (edited status, reverse created_at)
            return (edited, (created or "Z")[0]) 
            
        grp.sort(key=score, reverse=True)
        keep, rest = grp[0], grp[1:]
        out.append(keep)
        
        # The other items (in 'rest') are duplicates and are discarded.
        # We can set a flag if we add it to the FilingRecord.
        for r_rest in rest:
            r_rest.skip_reason = "duplicate_in_batch"
            
    return out
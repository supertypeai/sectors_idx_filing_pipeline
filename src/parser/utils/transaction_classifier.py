from __future__ import annotations
from typing import List, Dict, Optional, Tuple

import logging
logger = logging.getLogger(__name__)

# Canonical whitelist (exactly 9)
TAGS_WHITELIST = {
    #"bullish", "bearish", 
    "takeover", "investment", "divestment",
    "free-float-requirement", "MESOP", "inheritance", "share-transfer",
}

# Keyword banks for side-signals (parser may pass text here)
_KW_BUY = [
    "beli", "pembelian", "buy", "akumulasi", "investasi", "acquisition",
    "penambahan", "increase", "buyback", "buy back", "investment",
    "peningkatan"
]
_KW_SELL = [
    "jual", "penjualan", "sell", "divestasi", "divestment", "pengurangan",
    "reduksi", "disposal"
]
_KW_TRANSFER = [
    "transfer", "pemindahan", "konversi", "conversion", "neutral",
    "tanpa perubahan", "alih", "pengalihan"
]
_KW_INHERIT = ["waris", "inheritance", "hibah", "grant", "bequest"]
_KW_MESOP = ["mesop", "msop", "esop", "program opsi saham", "employee stock option"]
_KW_FREEFLOAT = ["free float", "free-float", "freefloat", "pemenuhan porsi publik"]
_KW_RESTRUCTURING = ["restrukturisasi", "restructuring", "reorganisasi"]
_KW_REPURCHASE = ['repo', 'penempatan saham revo']


def _crosses_50(before_pp: Optional[float], after_pp: Optional[float]) -> bool:
    try:
        b = float(before_pp)
        a = float(after_pp)
    except Exception:
        return False
    return (b < 50 <= a) or (b >= 50 > a)


def _safe_float(x, default=0.0) -> float:
    try:
        return float(x)
    except Exception:
        return default


def _any_kw(text_lower: str, bank: List[str]) -> bool:
    return any(k in text_lower for k in bank)


class TransactionClassifier:
    """
    Provides:
      - classify_transaction_type(): returns tx_type ('buy'/'sell'/'transfer'/'neutral') and PRELIM tags
      - compute_filings_tags(): final standardized tag list (whitelist-enforced)
    """

    @staticmethod
    def classify_transaction_type(
        text: str,
        pct_before: Optional[float],
        pct_after: Optional[float],
    ) -> Tuple[str, List[str]]:
        """
        Heuristic classification based on keywords + percentage delta.

        Returns:
            (tx_type, prelim_tags)
            prelim_tags already uses canonical tag vocabulary (no insider/ownership-change here).
        """
        tl = (text or "").lower()
        prelim: List[str] = []

        # Correction (we keep tx_type 'neutral', add no tags; downstream can still compute tags from data)
        if any(k in tl for k in ["perbaikan", "koreksi", "ralat", "errata", "amendment"]):
            return "neutral", []

        is_takeover = _crosses_50(pct_before, pct_after)
        if is_takeover:
            prelim.append("takeover")

        # Keyword-driven type
        if _any_kw(tl, _KW_SELL):
            return "sell", prelim
        if _any_kw(tl, _KW_BUY):
            return "buy", prelim
        if _any_kw(tl, _KW_TRANSFER) or _any_kw(tl, _KW_INHERIT):
            return "transfer", prelim

        # Fallback: derive from percentage movement
        try:
            b = float(pct_before) if pct_before is not None else None
            a = float(pct_after) if pct_after is not None else None
            if b is not None and a is not None:
                if a > b:
                    return "buy", prelim
                if a < b:
                    return "sell", prelim
        except Exception:
            pass

        return "neutral", prelim

    @staticmethod
    def detect_flags_from_text(text: str) -> Dict[str, bool]:
        """Lightweight flags for MESOP, free-float requirement, inheritance/transfer hints."""
        tl = (text or "").lower()
        return {
            "mesop": _any_kw(tl, _KW_MESOP),
            "free_float_requirement": _any_kw(tl, _KW_FREEFLOAT),
            "inheritance": _any_kw(tl, _KW_INHERIT),
            "share_transfer_hint": _any_kw(tl, _KW_TRANSFER),
            'capital-restructuring': _any_kw(tl, _KW_RESTRUCTURING)
        }

    @staticmethod
    def detect_tags_for_new_document(
        purpose: str,
        share_percentage_before: Optional[float],
        share_percentage_after: Optional[float],
        transaction_type: str,
    ) -> list[str]: 
        purpose = (purpose or '').lower()

        detect_tag = {
            "MESOP": _any_kw(purpose, _KW_MESOP),
            "free_float_requirement": _any_kw(purpose, _KW_FREEFLOAT),
            "inheritance": _any_kw(purpose, _KW_INHERIT),
            "share-transfer": _any_kw(purpose, _KW_TRANSFER),
            'capital-restructuring': _any_kw(purpose, _KW_RESTRUCTURING),
            'investment': _any_kw(purpose, _KW_BUY),
            'divestment': _any_kw(purpose, _KW_SELL),
            'repurchase-agreement': _any_kw(purpose, _KW_REPURCHASE),
        }
        
        tags = set()

        for tag, found in detect_tag.items(): 
            if found: 
                tags.add(tag)
        
        if not tags: 
            if transaction_type == 'buy':
                tags.add('investment')
            elif transaction_type == 'sell':
                tags.add('divestment')

        if 'investment' in tags and 'divestment' in tags: 
            if transaction_type == 'buy':
                tags.remove('divestment')
            elif transaction_type == 'sell':
                tags.remove('investment')

        if _crosses_50(share_percentage_before, share_percentage_after):
            tags.add("takeover")

        tags = list(tags)
        return sorted(tags)

    @staticmethod
    def compute_filings_tags(
        txns: List[Dict] | None,
        share_percentage_before: Optional[float],
        share_percentage_after: Optional[float],
        flags: Optional[Dict] = None,
    ) -> List[str]:
        """
        Final standardized tags for idx_filings, enforcing the 9-tag whitelist.
        txns: e.g. [{"type":"buy"|"sell"|"transfer", "amount": int|float}, ...]
        """
        flags = flags or {}
        tags = set()

        net_amount = 0.0
        has_buy = has_sell = False
        has_transfer = False
        explicit_inheritance = False

        for t in (txns or []):
            ttype = (t.get("type") or "").lower().strip()
            amt = _safe_float(t.get("amount"), 0.0)
            if ttype == "buy":
                has_buy = True
                net_amount += amt
            elif ttype == "sell":
                has_sell = True
                net_amount -= amt
            elif ttype == "transfer":
                has_transfer = True

        # investment/divestment/share-transfer(+inheritance)
        if has_buy and not has_sell:
            tags.add("investment")
        if has_sell and not has_buy:
            tags.add("divestment")
        if has_transfer or flags.get("share_transfer_hint"):
            tags.add("share-transfer")
        if flags.get("inheritance"):
            tags.add("inheritance")

        # bullish/bearish net
        # if net_amount > 0 and has_buy:
        #     tags.add("bullish")
        # elif net_amount < 0 and has_sell:
        #     tags.add("bearish")

        # takeover (50% crossing)
        if _crosses_50(share_percentage_before, share_percentage_after):
            tags.add("takeover")

        # Free float & MESOP
        if flags.get("free_float_requirement") or flags.get("free_float"):
            tags.add("free-float-requirement")
        if flags.get("mesop"):
            tags.add("MESOP")

        # Enforce whitelist & normalize
        clean = []
        for t in {t.lower().strip() for t in tags}:
            if t in TAGS_WHITELIST:
                clean.append(t)
        clean.sort()
        return clean

    @staticmethod
    def infer_direction(
        holding_before: Optional[int],
        holding_after: Optional[int],
        pct_before: Optional[float],
        pct_after: Optional[float]
    ) -> str:
        """
        Infer 'buy'/'sell'/'neutral' from holdings/percentages.
        """
        try:
            if isinstance(holding_before, (int, float)) and isinstance(holding_after, (int, float)):
                if holding_after > holding_before:
                    return "buy"
                if holding_after < holding_before:
                    return "sell"
        except Exception:
            pass
        try:
            if isinstance(pct_before, (int, float)) and isinstance(pct_after, (int, float)):
                if pct_after > pct_before:
                    return "buy"
                if pct_after < pct_before:
                    return "sell"
        except Exception:
            pass
        return "neutral"

    @staticmethod
    def mismatch_flag(
        doc_type: Optional[str],
        inferred: str,
        holding_before: Optional[int],
        holding_after: Optional[int],
        pct_before: Optional[float],
        pct_after: Optional[float]
    ) -> Optional[Dict]:
        """
        Returns a payload when document label conflicts with inferred direction (observability).
        """
        doc = (doc_type or "").strip().lower()
        if doc in {"buy", "sell"} and inferred in {"buy", "sell"} and doc != inferred:
            return {
                "type": "Transaction Type Mismatch",
                "message": f"Document says '{doc}', but holdings/percentages imply '{inferred}'.",
                "document_type": doc,
                "inferred_type": inferred,
                "holding_before": holding_before,
                "holding_after": holding_after,
                "share_percentage_before": pct_before,
                "share_percentage_after": pct_after,
            }
        return None

    @staticmethod
    def validate_direction(
        before: Optional[float],
        after: Optional[float],
        tx_type: str,
        eps: float = 1e-3
    ) -> Tuple[bool, Optional[str]]:
        """
        Sanity for direction vs percentages.
        """
        try:
            b = float(before) if before is not None else None
            a = float(after) if after is not None else None
        except Exception:
            return False, "non_numeric_before_after"
        if b is None or a is None:
            return False, "missing_before_or_after"

        t = (tx_type or "").strip().lower()
        if t == "buy" and a + eps < b:
            return False, f"inconsistent_buy: after({a}) < before({b})"
        if t == "sell" and a > b + eps:
            return False, f"inconsistent_sell: after({a}) > before({b})"
        return True, None

    @staticmethod
    def coherent_or_reason(
        tx_type: Optional[str],
        pct_before: Optional[float],
        pct_after: Optional[float],
        eps: float = 1e-3
    ) -> Tuple[bool, Optional[str]]:
        t = (tx_type or "").lower()
        if t in {"buy", "sell"}:
            return TransactionClassifier.validate_direction(pct_before, pct_after, t, eps=eps)
        return True, None

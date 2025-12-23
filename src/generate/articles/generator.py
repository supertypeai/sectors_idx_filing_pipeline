# src/generate/articles/generator.py
from __future__ import annotations
from typing import Dict, Any, Optional, List, Tuple
from datetime import datetime, timezone
import json
import os
import re
from pathlib import Path, PurePath

from .utils.company import CompanyCache
from .utils.extractor import extract_info_from_text
from .utils.summarizer import Summarizer
from .utils.classifier import Classifier
from .utils.schema import Article, coerce_timestamp_iso
from .utils.io_utils import get_logger

log = get_logger(__name__)

# helpers (without kebab/slug)
def _with_jk(sym: Optional[str]) -> Optional[str]:
    if not sym:
        return sym
    s = str(sym).strip().upper()
    if not s:
        return None
    if "." in s:
        return s
    return f"{s}.JK"

def _dedup_preserve(seq: List[str]) -> List[str]:
    seen = set()
    out: List[str] = []
    for x in seq:
        if x is None:
            continue
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out

def _ensure_list(x: Any) -> List[str]:
    if x is None:
        return []
    if isinstance(x, list):
        return x
    return [x]

def _coerce_float(x) -> Optional[float]:
    try:
        f = float(x)
        return f if f > 0 else None
    except Exception:
        return None

def _coerce_int(x) -> Optional[int]:
    try:
        i = int(str(x).strip())
        return i if i >= 0 else None
    except Exception:
        return None

def _parse_price_transaction(pt: Any) -> Tuple[List[float], List[int]]:
    if pt is None:
        return [], []
    try:
        obj = pt
        if isinstance(pt, str):
            obj = json.loads(pt)
        if isinstance(obj, dict):
            prices_raw = obj.get("prices") or []
            amts_raw = obj.get("amount_transacted") or obj.get("amounts") or []
            prices = [p for p in (_coerce_float(v) for v in prices_raw) if p is not None]
            amts = [a for a in (_coerce_int(v) for v in amts_raw) if a is not None]
            if len(prices) != len(amts):
                m = min(len(prices), len(amts))
                prices, amts = prices[:m], amts[:m]
            return prices, amts
    except Exception:
        pass
    return [], []

def _extract_prices_amounts_from_filing(f: Dict[str, Any]) -> Tuple[List[float], List[int]]:
    prices, amts = _parse_price_transaction(f.get("price_transaction"))
    if prices and amts:
        return prices, amts
    arr_p = f.get("prices") or []
    arr_a = f.get("amount_transacted") or f.get("amounts") or []
    arr_p = [p for p in (_coerce_float(v) for v in arr_p) if p is not None]
    arr_a = [a for a in (_coerce_int(v) for v in arr_a) if a is not None]
    if arr_p and arr_a:
        m = min(len(arr_p), len(arr_a))
        return arr_p[:m], arr_a[:m]
    single_p = _coerce_float(f.get("price"))
    single_a = _coerce_int(f.get("amount_transaction"))
    prices = [single_p] if single_p is not None else []
    amts = [single_a] if single_a is not None else []
    return prices, amts

def _fmt_int(n: int) -> str:
    try:
        return f"{int(n):,}".replace(",", ".")
    except Exception:
        return str(n)

def _fmt_idr(n: float) -> str:
    try:
        return "IDR " + f"{int(round(n)):,}".replace(",", ".")
    except Exception:
        return f"IDR {n}"

def _date_str(ts: Optional[str]) -> str:
    if not ts:
        return ""
    try:
        if "T" in ts:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        else:
            dt = datetime.fromisoformat(ts)
        return dt.strftime("%B %d, %Y")
    except Exception:
        return ts

def _date_str_wib(ts: Optional[str]) -> str:
    if not ts:
        return ""
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.strftime("%B %d, %Y") + " (WIB)"
    except Exception:
        return f"{ts} (WIB)"

def _opening_sentence(facts: Dict[str, Any]) -> str:
    pub = facts.get("announcement_published_at")
    human = _date_str_wib(pub) if pub else None
    if human:
        return f"According to the published announcement on {human}, "
    return "According to the published announcement, "

def _strip_redundant_on_date(text: str, pub_ts: Optional[str]) -> str:
    """
    Remove leading 'On <Month DD, YYYY>, ' at the start of text if it matches the announcement date.
    """
    if not text or not pub_ts:
        return text
    ds = _date_str(pub_ts)  # 'October 08, 2025'
    if not ds:
        return text
    pat = re.compile(rf'^\s*on\s+{re.escape(ds)}\s*,\s*', re.IGNORECASE)
    return pat.sub("", text, count=1)

def _to_narrative_if_keyfacts(title: str, body: str, facts: Dict[str, Any]) -> Tuple[str, str]:
    if not body or not body.lstrip().lower().startswith("key facts"):
        return title, body

    cname = facts.get("company_name") or ""
    sym = facts.get("symbol") or ""
    tx = (facts.get("transaction_type") or "transaction").lower()
    holder = facts.get("holder_name") or facts.get("holder_type") or "an insider"
    opening = _opening_sentence(facts)

    prices: List[float] = facts.get("prices") or []
    amounts: List[int] = facts.get("amount_transacted") or []

    vol = sum(int(a) for a in amounts if a is not None) if amounts else 0

    if prices:
        pmin, pmax = min(prices), max(prices)
        price_phrase = (
            f"with prices ranging from {_fmt_idr(pmin)} to {_fmt_idr(pmax)} per share"
            if pmax != pmin else
            f"at approximately {_fmt_idr(pmin)} per share"
        )
    else:
        price_phrase = "at an undisclosed price"

    hb = facts.get("holdings_before")
    ha = facts.get("holdings_after")
    own = ""
    if hb and ha:
        try:
            b, a = int(hb), int(ha)
            trend = "increasing" if a > b else ("decreasing" if a < b else "keeping")
            own = f" This {trend} the stake from {_fmt_int(b)} to {_fmt_int(a)} shares."
        except Exception:
            own = f" The filing notes a change in ownership from {hb} to {ha}."

    p1_core = f"{holder} {tx} " + (f"{_fmt_int(vol)} shares " if vol > 0 else "shares ")
    p1 = f"{p1_core}of {cname} ({sym}), {opening.rstrip()}{own}"

    reason = facts.get("reason")
    p2_extra = f" Stated purpose: {reason}." if reason else ""
    p2 = f"The transaction was executed {price_phrase}.{p2_extra}"

    p3 = "This disclosure is informational and not investment advice. Please refer to the official filing for complete details."

    new_body = f"{p1}\n\n{p2}\n\n{p3}"
    return title, new_body

# Download meta mapping (filename -> URL)
def _basename(s: Optional[str]) -> Optional[str]:
    if not s:
        return None
    try:
        return PurePath(str(s)).name
    except Exception:
        return None

def _build_download_index(path: str) -> Dict[str, str]:
    """
    Expect a JSON list:
      [{"filename":"x.pdf","url":"https://.../x.pdf", ...}, ...]
    Returns: { "x.pdf": "https://.../x.pdf" } (lowercased keys)
    """
    idx: Dict[str, str] = {}
    p = Path(path)
    if not p.exists():
        return idx
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return idx
    if not isinstance(data, list):
        return idx
    for item in data:
        if not isinstance(item, dict):
            continue
        fn = _basename(item.get("filename")) or _basename(item.get("url"))
        url = item.get("url")
        if fn and url:
            idx[fn.lower()] = str(url)
    return idx

def _candidate_filenames(row: Dict[str, Any]) -> List[str]:
    cands: List[str] = []
    for k in ("source", "pdf", "pdf_url", "url", "attachment_url", "filename"):
        fn = _basename(row.get(k))
        if fn:
            cands.append(fn)
    for nest in ("announcement", "announcement_meta"):
        blk = row.get(nest)
        if isinstance(blk, dict):
            for k in ("pdf_url", "url", "attachment_url", "filename"):
                fn = _basename(blk.get(k))
                if fn:
                    cands.append(fn)
    # de-dup
    out, seen = [], set()
    for n in cands:
        nl = n.lower()
        if nl not in seen:
            seen.add(nl)
            out.append(n)
    return out


class ArticleGenerator:
    def __init__(
        self,
        company_map_path: str = "data/company/company_map.json",
        latest_prices_path: str = "data/company/company_map.json",
        use_llm: bool = False,
        groq_model: str = "llama-3.3-70b-versatile",
        prefer_symbol: bool = True,
        provider: Optional[str] = None,
        downloads_meta_path: str = "data/downloaded_pdfs.json",
    ):
        self.company = CompanyCache(company_map_path, latest_prices_path)
        self.summarizer = Summarizer(use_llm=use_llm, groq_model=groq_model, provider=provider)
        self.classifier = Classifier(use_llm=use_llm, model_name=groq_model, provider=provider)
        self.prefer_symbol = prefer_symbol
        # filename -> url index
        self._dl_index = _build_download_index(downloads_meta_path)

    # map filename/partial to full URL using downloads index
    def _map_source_url(self, row: Dict[str, Any]) -> str:
        # 1) prefer explicit PDF URL if present
        for k in ("pdf_url", "url", "attachment_url", "source"):
            v = row.get(k)
            if isinstance(v, str) and "://" in v and v.lower().endswith(".pdf"):
                return v
        for nest in ("announcement", "announcement_meta"):
            blk = row.get(nest)
            if isinstance(blk, dict):
                for k in ("pdf_url", "url", "attachment_url"):
                    v = blk.get(k)
                    if isinstance(v, str) and "://" in v and v.lower().endswith(".pdf"):
                        return v

        # 2) resolve via downloads index by filename
        for fn in _candidate_filenames(row):
            key = fn.lower()
            if key in self._dl_index:
                return self._dl_index[key]

        # 3) fallback
        return str(row.get("source") or row.get("pdf_url") or "")

    def _enrich_company(self, symbol: Optional[str]) -> Dict[str, Any]:
        if not symbol:
            return {"company_name": "", "sector": "", "sub_sector": []}
        info = self.company.get(symbol) or {}
        if (not info) and ("." not in str(symbol)):
            info = self.company.get(f"{symbol}.JK") or {}
        return {
            "company_name": info.get("company_name", ""),
            "sector": info.get("sector", ""),
            "sub_sector": _ensure_list(info.get("sub_sector", [])),
        }

    def _finalize(self, core: Dict[str, Any]) -> Dict[str, Any]:
        ts = core.get("timestamp")
        if not ts:
            core["timestamp"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        else:
            core["timestamp"] = coerce_timestamp_iso(ts)

        tickers = core.get("tickers") or []
        symbol = core.get("symbol")
        symbol = _with_jk(core.get("symbol"))
        tickers_raw = core.get("tickers") or []
        tickers_norm = _dedup_preserve([_with_jk(t) for t in tickers_raw if t])
        if self.prefer_symbol:
            if not symbol and tickers_norm:
                core["symbol"] = tickers[0]
            if symbol and not tickers_norm:
                core["tickers"] = [symbol]
        else:
            if not tickers_norm and symbol:
                core["tickers"] = [symbol]

        core["symbol"] = symbol
        core["tickers"] = tickers_norm

        # Articles prefer list-style sub_sector
        core["sub_sector"] = _ensure_list(core.get("sub_sector", []))
        core.setdefault("dimension", {})
        core.setdefault("score", 0.0)
        return core

    # From filings
    def from_filing(self, filing: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        # pdf_url = self._map_source_url(filing)
        # symbol_raw = (filing.get("symbol") or (filing.get("tickers") or [None])[0])
        # symbol = _with_jk(symbol_raw)
        # meta = self._enrich_company(symbol)

        # holdings_before = (
        #     filing.get("holdings_before") or filing.get("holding_before")
        #     or filing.get("previous_holding") or filing.get("prev_holding")
        # )
        # holdings_after = (
        #     filing.get("holdings_after") or filing.get("holding_after")
        #     or filing.get("new_holding") or filing.get("post_holding")
        # )
        # # reason = filing.get("reason") or filing.get("purpose") or filing.get("objective")

        # # prices, amounts = _extract_prices_amounts_from_filing(filing)
        # # filing_tickers = filing.get("tickers") or []
        # # tickers_norm = _dedup_preserve([_with_jk(t) for t in filing_tickers if t])

        # # if symbol and symbol not in tickers_norm:
        # #     tickers_norm.insert(0, symbol)

        # # print(f'filing for article input: {json.dumps(filing, indent=2)}')

        # facts = {
        #     "symbol": symbol,
        #     # "tickers": tickers_norm,
        #     "company_name": filing.get("company_name") or meta["company_name"],
        #     "sector": filing.get("sector") or meta["sector"],
        #     "sub_sector": filing.get("sub_sector") or meta["sub_sector"],
        #     "holder_type": filing.get("holder_type") or "",
        #     "holder_name": filing.get("holder_name") or "",
        #     "transaction_type": filing.get("transaction_type") or filing.get("type") or "",
        #     # "prices": prices,
        #     # "amount_transacted": amounts,
        #     "holdings_before": holdings_before,
        #     "holdings_after": holdings_after,
        #     # "reason": reason,
        #     "timestamp": filing.get("timestamp"),
        #     "source": pdf_url,
        #     "announcement_published_at": filing.get("announcement_published_at"),
        # }

        log.info('generating title body')
        title, body = self.summarizer.summarize_from_facts(filing)
        print(f'title: {title}')
        print(f'\nbody: {body}')
        # title, body = _to_narrative_if_keyfacts(title, body, facts)

        # if not body.lstrip().lower().startswith("according to the published announcement"):
        #     # Remove 'On <date>, ' if it matches the announcement date
        #     body = _strip_redundant_on_date(body, facts.get("announcement_published_at"))
        #     opening = _opening_sentence(facts)
        #     body = f"{opening}{body}"

        tags = self.classifier.infer_tags(filing, text_hint=None)
        sentiment = self.classifier.infer_sentiment(filing, text_hint=None)
        print(f'\ntags: {tags}, sentiment: {sentiment}')

        # tags = 'test'
        # sentiment = 'test'
        symbol = filing.get("symbol") or [] 
        tickers = _dedup_preserve([symbol])

        article = Article(
            title=title, body=body, source=filing.get('source'),
            timestamp=filing["timestamp"],
            company_name=filing["company_name"],
            # symbol=filing["symbol"],
            tickers=tickers,
            sector=filing["sector"],
            sub_sector=_ensure_list(filing["sub_sector"]),
            tags=tags, sentiment=sentiment,
            dimension={},
            score=0.0,
        ).to_dict()

        article["announcement_published_at"] = filing.get("announcement_published_at", '')

        return self._finalize(article)

    # FROM TEXT
    def from_text_item(self, item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        text = item.get("text") or ""
        if not text.strip():
            return None
        pdf_url = self._map_source_url(item)
        symbol = _with_jk(item.get("symbol"))
        extracted = extract_info_from_text(text)
        sym = symbol or _with_jk(extracted.get("symbol"))
        meta = self._enrich_company(sym)

        tickers_norm = [sym] if sym else []

        facts = {
            "symbol": sym,
            "tickers": tickers_norm,
            "company_name": item.get("company_name") or meta["company_name"],
            "sector": item.get("sector") or meta["sector"],
            "sub_sector": item.get("sub_sector") or meta["sub_sector"],
            "holder_type": item.get("holder_type") or extracted.get("holder_type") or "",
            "holder_name": item.get("holder_name") or extracted.get("holder_name") or "",
            "transaction_type": item.get("transaction_type") or extracted.get("transaction_type") or "",
            "prices": item.get("prices") or extracted.get("prices") or [],
            "amount_transacted": item.get("amount_transacted") or extracted.get("amount_transacted") or [],
            "holdings_before": item.get("holdings_before") or item.get("holding_before"),
            "holdings_after": item.get("holdings_after") or item.get("holding_after"),
            "reason": item.get("reason") or item.get("purpose"),
            "timestamp": item.get("timestamp"),
            "source": pdf_url,
            "announcement_published_at": item.get("announcement_published_at"),
        }

        title, body = self.summarizer.summarize_from_facts(facts, text_hint=text)
        title, body = _to_narrative_if_keyfacts(title, body, facts)

        if not body.lstrip().lower().startswith("according to the published announcement"):
            body = _strip_redundant_on_date(body, facts.get("announcement_published_at"))
            opening = _opening_sentence(facts)
            body = f"{opening}{body}"

        tags = self.classifier.infer_tags(facts, text_hint=text)
        sentiment = self.classifier.infer_sentiment(facts, text_hint=text)

        article = Article(
            title=title, body=body, source=pdf_url,
            timestamp=facts["timestamp"],
            company_name=facts["company_name"],
            symbol=facts["symbol"],
            tickers=facts["tickers"],
            sector=facts["sector"],
            sub_sector=_ensure_list(facts["sub_sector"]),
            tags=tags, sentiment=sentiment,
            dimension={},
            score=0.0,
        ).to_dict()

        article["announcement_published_at"] = facts.get("announcement_published_at")

        return self._finalize(article)

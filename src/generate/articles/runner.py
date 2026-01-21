from __future__ import annotations

from typing import List, Dict, Any, Optional
from .generator import ArticleGenerator
from .utils.io_utils import get_logger


LOGGER = get_logger(__name__)


def run_from_filings(
    filings: List[Dict[str, Any]],
    company_map_path: str = "data/company/company_map.json",
    latest_prices_path: str = "data/company/company_map.json",
    use_llm: bool = False,
    model_name: str = "llama-3.3-70b-versatile",  # Not used in summary
    prefer_symbol: bool = True,
    provider: Optional[str] = None,  # Not used in sumamry 
) -> List[Dict[str, Any]]:
    gen = ArticleGenerator(
        company_map_path=company_map_path,
        latest_prices_path=latest_prices_path,
        use_llm=use_llm,
        groq_model=model_name,
        prefer_symbol=prefer_symbol,
        provider=provider,
    )
    out: List[Dict[str, Any]] = []
    for filing in filings:
        try:
            art = gen.from_filing(filing)
            if art:
                out.append(art)
        except Exception as error:
            LOGGER.exception(f"Error generating article from filing: {error}")
    return out

def run_from_text_items(
    items: List[Dict[str, Any]],
    company_map_path: str = "data/company/company_map.json",
    latest_prices_path: str = "data/company/company_map.json",
    use_llm: bool = False,
    model_name: str = "llama-3.3-70b-versatile",
    prefer_symbol: bool = True,
    provider: Optional[str] = None,
) -> List[Dict[str, Any]]:
    gen = ArticleGenerator(
        company_map_path=company_map_path,
        latest_prices_path=latest_prices_path,
        use_llm=use_llm,
        groq_model=model_name,
        prefer_symbol=prefer_symbol,
        provider=provider,
    )
    out: List[Dict[str, Any]] = []
    for item in items:
        try:
            art = gen.from_text_item(item)
            if art:
                out.append(art)
        except Exception as error:
            LOGGER.exception(f"Error generating article from text item: {error}")
    return out

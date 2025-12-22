# src/generate/articles/utils/classifier.py
from __future__ import annotations
import os
from typing import Dict, Any, List, Optional
# from pydantic import BaseModel, Field 
# from google import genai
# from google.genai import types 

from .io_utils import get_logger
log = get_logger(__name__)

# Heuristic sets
BUY = {"buy", "pembelian", "acquire", "acquisition", "pembelian saham"}
SELL = {"sell", "penjualan", "dispose", "divest", "penjualan saham"}
TRANSFER = {"transfer", "pengalihan", "alih", "off-market transfer"}

# Model normalization helpers
# Map deprecated -> recommended
# DEPRECATED_MAP = {
#     "llama-3.1-70b-versatile": "llama-3.3-70b-versatile",
#     "llama3-70b-8192": "llama-3.3-70b-versatile",
#     "llama3-8b-8192": "llama-3.1-8b-instant",
# }

# # Friendly aliases for Groq models
# GROQ_ALIAS = {
#     "compound": "groq/compound",
#     "compound-mini": "groq/compound-mini",
#     "groq/compound-mini": "groq/compound-mini",
#     "groq/compound": "groq/compound",
# }

# def _env(key: str, default: str = "") -> str:
#     v = os.getenv(key)
#     if v is None:
#         return default
#     return v.strip().strip('"').strip("'")

# def _pick_provider(explicit: Optional[str]) -> str:
#     """
#     Resolve provider priority:
#     1) explicit arg
#     2) LLM_PROVIDERS (first item) or LLM_PROVIDER env
#     3) if GROQ_API_KEY exists -> groq, elif OPENAI_API_KEY -> openai, elif GEMINI_API_KEY/GOOGLE_API_KEY -> gemini, else openai
#     """
#     if explicit:
#         return explicit.lower().strip().strip('"').strip("'")

#     env_list = (_env("LLM_PROVIDERS") or "")
#     if env_list:
#         return env_list.split(",")[0].strip().lower()

#     env = _env("LLM_PROVIDER").lower()
#     if env in {"openai", "groq", "gemini"}:
#         return env

#     if _env("GROQ_API_KEY"):
#         return "groq"
#     if _env("OPENAI_API_KEY"):
#         return "openai"
#     if _env("GEMINI_API_KEY") or _env("GOOGLE_API_KEY"):
#         return "gemini"
#     return "openai"

# def _normalize_model(provider: str, name: Optional[str]) -> str:
#     """
#     Keep caller intent:
#     - If name is provided and recognized as deprecated, upgrade via DEPRECATED_MAP.
#     - If name is provided and NOT deprecated, keep it as-is.
#     - If name is missing, use sensible provider-specific default.
#     - For Groq, also accept friendly aliases ("compound" -> "groq/compound").
#     - For OpenAI, if user mistakenly passes a llama-* name, swap to gpt-4.1-mini.
#     """
#     provider = (provider or "").strip().lower()
#     raw = (name or "").strip()

#     if provider == "openai":
#         if not raw:
#             return "gpt-4.1-mini"
#         # prevent llama-* under openai
#         if raw.startswith("llama-") or raw.startswith("llama3"):
#             return "gpt-4.1-mini"
#         # keep user-provided non-llama model
#         return raw

#     if provider == "gemini":
#         if not raw:
#             return "gemini-1.5-flash"
#         return raw

#     if provider == "groq":
#         if not raw:
#             return "llama-3.3-70b-versatile"
#         # expand alias if any
#         if raw in GROQ_ALIAS:
#             raw = GROQ_ALIAS[raw]
#         # upgrade deprecated ids, otherwise keep as-is
#         return DEPRECATED_MAP.get(raw, raw)

#     # unknown provider -> return what we got or empty
#     return raw or ""

# def _init_llm(provider: str, model_name: str):
#     """
#     Create a LangChain chat model bound to the requested provider/model.
#     Returns a tuple: (llm, effective_model_name).
#     """
#     provider = (provider or "").strip().lower()
#     eff_model = _normalize_model(provider, model_name)

#     if provider == "openai":
#         from langchain_openai import ChatOpenAI
#         key = _env("OPENAI_API_KEY")
#         if not key:
#             raise EnvironmentError("OPENAI_API_KEY missing for classifier LLM.")
#         try:
#             llm = ChatOpenAI(model=eff_model or "gpt-4.1-mini", temperature=0.0, api_key=key)
#         except TypeError:
#             # older langchain_openai signatures
#             llm = ChatOpenAI(model=eff_model or "gpt-4.1-mini", temperature=0.0)
#         return llm, (eff_model or "gpt-4.1-mini")

#     if provider == "groq":
#         from langchain_groq import ChatGroq
#         key = _env("GROQ_API_KEY")
#         if not key:
#             raise EnvironmentError("GROQ_API_KEY missing for classifier LLM.")

#         # Optional: allow base URL override if your environment uses a proxy or custom gateway.
#         # ChatGroq currently ignores base_url in most versions, but we pass it if supported.
#         base_url = _env("GROQ_BASE_URL", "")
#         kwargs = dict(model=eff_model, temperature=0.0)
#         if base_url:
#             kwargs["base_url"] = base_url

#         try:
#             llm = ChatGroq(groq_api_key=key, **kwargs)
#         except TypeError:
#             # older langchain_groq signatures
#             llm = ChatGroq(api_key=key, **kwargs)

#         return llm, eff_model

#     if provider == "gemini":
#         # Your project-specific Gemini wrapper (preferred)
#         try:
#             from ..model.llm_gemini import GeminiLLM  # noqa
#             key = _env("GEMINI_API_KEY") or _env("GOOGLE_API_KEY")
#             if not key:
#                 raise EnvironmentError("GEMINI_API_KEY/GOOGLE_API_KEY missing for classifier LLM.")
#             llm = GeminiLLM(api_key=key, model=eff_model, temperature=0.0, max_output_tokens=128)
#             return llm, eff_model
#         except Exception as e:
#             # Optional fallback to LangChain's Google wrapper if you have it
#             try:
#                 from langchain_google_genai import ChatGoogleGenerativeAI
#                 key = _env("GEMINI_API_KEY") or _env("GOOGLE_API_KEY")
#                 if not key:
#                     raise EnvironmentError("GEMINI_API_KEY/GOOGLE_API_KEY missing for classifier LLM.")
#                 llm = ChatGoogleGenerativeAI(model=eff_model, temperature=0.0, api_key=key)
#                 return llm, eff_model
#             except Exception:
#                 raise e

#     raise ValueError(f"Unknown provider: {provider}")


class Classifier:
    """
    Heuristics-first classifier with optional LLM refinement.
    By default, respects the CLI's --provider and --model.
    You can override the model via env: CLASSIFIER_MODEL
    """
    def __init__(self, use_llm: bool = False, model_name: str = "gpt-4.1-mini", provider: Optional[str] = None):
        self.use_llm = use_llm
        # self.provider = _pick_provider(provider)

        # # Allow environment override, but keep CLI as the base.
        # env_override = _env("CLASSIFIER_MODEL") or None
        # chosen_model = env_override if env_override else model_name

        # self.model_name = chosen_model
        self._llm = None

        # if self.use_llm:
        #     try:
        #         self._llm, eff_model = _init_llm(self.provider, self.model_name)
        #         log.info(f"Classifier using {self.provider} model: {eff_model}")
        #     except Exception as e:
        #         log.warning(f"Classifier LLM init failed; fallback to heuristics. {e}")
        #         self.use_llm = False


    # Tag inference
    def infer_tags(self, facts: Dict[str, Any], text_hint: Optional[str]) -> List[str]:
        tx = (facts.get("transaction_type") or "").lower()
        tags = ["Insider Trading"]
        return tags


    # Sentiment inference
    def infer_sentiment(self, facts: Dict[str, Any], text_hint: Optional[str]) -> str:
        tx = (facts.get("transaction_type") or "").lower()
        if tx in BUY:
            return "positive"
        if tx in SELL:
            return "negative"
        if tx in TRANSFER:
            return "neutral"

        # Optional LLM refinement from text_hint
        if self.use_llm and self._llm and text_hint:
            try:
                prompt = (
                    "Classify sentiment strictly as one of: positive, negative, or neutral, "
                    "based only on the text:\n" + text_hint[:800]
                )
                msg = self._llm.invoke(prompt)
                s = (getattr(msg, "content", "") or "").strip().lower()
                if s in ("positive", "negative", "neutral"):
                    return s
            except Exception as e:
                log.debug(f"LLM sentiment failed: {e}")
        return "neutral"

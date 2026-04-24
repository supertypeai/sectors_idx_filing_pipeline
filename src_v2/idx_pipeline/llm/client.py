from langchain.chat_models import init_chat_model
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import BaseMessage
from langchain_core.outputs import ChatResult
from langchain_core.callbacks import BaseCallbackHandler

from idx_pipeline.config.settings import GROQ_API_KEY_DEV, GEMINI_API_KEY, GEMINI_API_KEY_BACKUP
from idx_pipeline.utils.constant import * 

import logging 


LOGGER = logging.getLogger(__name__)


class TokenUsageLogger(BaseCallbackHandler):
    def on_llm_end(self, response, **kwargs):
        token_usage = response.llm_output.get("token_usage", {}) if response.llm_output else {}
        completion_details = token_usage.get("completion_tokens_details") or {}

        LOGGER.info(
            "token usage: prompt=%d completion=%d reasoning=%d total=%d finish_reason=%s",
            token_usage.get("prompt_tokens", 0),
            token_usage.get("completion_tokens", 0),
            completion_details.get("reasoning_tokens", 0),
            token_usage.get("total_tokens", 0),
            response.generations[0][0].generation_info.get("finish_reason", "unknown")
            if response.generations else "unknown",
        )


def extract_status_code(error: Exception) -> int | None:
    status_code = getattr(error, "status_code", None)
    if status_code is not None:
        return int(status_code)

    for token in str(error).split():
        if token.isdigit() and len(token) == 3:
            return int(token)

    return None


def classify_error(error: Exception) -> str:
    """
    Returns one of three actions:
      'rotate' -> key-level problem, try the next key
      'abort'  -> request-level or server-level problem, rotating will not help
      'raise'  -> unexpected error, propagate immediately
    """
    status_code = extract_status_code(error)
    error_message = str(error).lower()

    if status_code == 400 and any(keyword in error_message for keyword in ROTATE_400_KEYWORDS):
        return "rotate"
    
    if status_code in ROTATE_STATUS_CODES:
        return "rotate"

    if status_code in ABORT_STATUS_CODES:
        return "abort"

    if any(keyword in error_message for keyword in ROTATE_KEYWORDS):
        return "rotate"

    if any(keyword in error_message for keyword in ABORT_KEYWORDS):
        return "abort"

    return "raise"


class KeyRotatingChatModel(BaseChatModel):
    """
    Wraps a pool of LLM clients initialised with different API keys for the
    same model. On a key-level failure (429, 401, 403) it transparently
    rotates to the next available key. On request-level or server-level
    failures it raises immediately without wasting the remaining keys.
    """
    llm_pool: list[BaseChatModel]
    model_name_identifier: str

    class Config:
        arbitrary_types_allowed = True

    @property
    def _llm_type(self) -> str:
        return f"key-rotating-{self.model_name_identifier}"

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        **kwargs: any,
    ) -> ChatResult:
        last_error: Exception | None = None

        for index, llm_client in enumerate(self.llm_pool):
            try:
                return llm_client._generate(messages, stop=stop, **kwargs)
            
            except Exception as error:
                action = classify_error(error)

                if action == "rotate":
                    LOGGER.warning(
                        f"Key index {index} failed for '{self.model_name_identifier}' "
                        f"(rotating to next key). Error: {error}"
                    )
                    last_error = error
                    continue

                if action == "abort":
                    LOGGER.error(
                        f"Non-recoverable error for '{self.model_name_identifier}', "
                        f"aborting key rotation. Error: {error}"
                    )
                    raise

                raise

        raise RuntimeError(
            f"All {len(self.llm_pool)} API keys exhausted for model "
            f"'{self.model_name_identifier}'. Last error: {last_error}"
        )

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        **kwargs: any,
    ) -> ChatResult:
        last_error: Exception | None = None

        for index, llm_client in enumerate(self.llm_pool):
            try:
                return await llm_client._agenerate(messages, stop=stop, **kwargs)
            
            except Exception as error:
                action = classify_error(error)

                if action == "rotate":
                    LOGGER.warning(
                        f"Key index {index} failed for '{self.model_name_identifier}' "
                        f"(async, rotating to next key). Error: {error}"
                    )
                    last_error = error
                    continue

                if action == "abort":
                    LOGGER.error(
                        f"Non-recoverable error for '{self.model_name_identifier}' "
                        f"(async), aborting key rotation. Error: {error}"
                    )
                    raise

                raise

        raise RuntimeError(
            f"All {len(self.llm_pool)} API keys exhausted for model "
            f"'{self.model_name_identifier}'. Last error: {last_error}"
        )
    

def get_llm(model_name: str, temperature: float = 0.5): 
    config_model = MODEL_CONFIG.get(model_name)

    if config_model is None:
        available_models = ', '.join(MODEL_CONFIG.keys())
        LOGGER.error(f"Unknown model name: '{model_name}'. Available models: {available_models}")
        return None
    
    provider = config_model.get('provider')
    
    if provider == 'groq': 
        api_keys = [GROQ_API_KEY_DEV]
        
    elif provider == 'google-genai': 
        api_keys = [GEMINI_API_KEY, GEMINI_API_KEY_BACKUP]

    else:
        LOGGER.error(f"Unsupported provider: '{provider}'")
        return None
    
    api_keys = [key for key in api_keys if key]
    
    if not api_keys:
        LOGGER.error(f"No valid API keys found for provider: '{provider}'")
        return None
    
    llm_pool = []
    
    for api_key in api_keys:
        try:
            initiate_model = init_chat_model(
                config_model.get('model'),
                model_provider=provider,
                temperature=temperature,
                max_retries=3,
                api_key=api_key,
                max_tokens=18000 
            ) 

            llm_pool.append(initiate_model)
    
        except Exception as error:
            LOGGER.error(f'Error initialize llm: {error}')
            continue 
    
    if not llm_pool:
        LOGGER.error(f"No clients could be initialized for '{model_name}'")
        return None

    return KeyRotatingChatModel(llm_pool=llm_pool, model_name_identifier=model_name)
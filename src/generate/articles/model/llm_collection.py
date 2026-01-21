from langchain.chat_models import init_chat_model
from dotenv import load_dotenv

import os 

load_dotenv(override=True)


class LLMCollection:
    """
    @brief Singleton class to manage a collection of LLM (Large Language Model) instances.
    This class ensures that only one instance of the LLMCollection exists and provides methods to add and retrieve LLM instances.
    """
    _instance = None
    
    def __new__(cls):
        """
        @brief Creates a new instance of LLMCollection if it doesn't already exist.
        @return The singleton instance of LLMCollection.
        """
        if cls._instance is None:
            cls._instance = super(LLMCollection, cls).__new__(cls)

            model_providers = {
                "openai/gpt-oss-120b": "groq",
                "gemini-2.5-flash": "google_genai",
                "openai/gpt-oss-20b": "groq",
            }

            groq_api_keys = os.getenv("GROQ_API_KEY", "")
            gemini_api_key = os.getenv("GEMINI_API_KEY", "")
            gemini_api_key_backup = os.getenv("GEMINI_API_KEY_BACKUP", "")

            gemini_api_keys = [gemini_api_key, gemini_api_key_backup]

            llms= []
            for model, provider in model_providers.items():
                if provider == 'groq':
                   llms.append(
                        init_chat_model(
                            model,
                            model_provider=provider,
                            temperature=0.7,
                            max_retries=3,
                            api_key=groq_api_keys,
                        )
                    )
                    
                elif provider == 'google_genai':
                     for gemini_key in gemini_api_keys:
                        llms.append(
                            init_chat_model(
                                model,
                                model_provider=provider,
                                temperature=0.5,
                                max_retries=3,
                                api_key=gemini_key,
                            )
                        )


            cls._instance._llms = llms 

        return cls._instance

    def add_llm(self, llm):
        """
        @brief Adds a new LLM instance to the collection.
        @param llm The LLM instance to be added to the collection.
        """
        self._llms.append(llm)

    def get_llms(self):
        """
        @brief Retrieves the list of LLM instances in the collection.
        @return A list of LLM instances.
        """
        return self._llms

# Example usage:
# llm_collection = LLMCollection()
# llm_collection.add_llm("LLM1")
# llm_collection.add_llm("LLM2")
# print(llm_collection.get_llms())
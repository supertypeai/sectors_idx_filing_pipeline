from deep_translator import GoogleTranslator
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import JsonOutputParser

from idx_pipeline.llm.client import get_llm
from idx_pipeline.llm.prompts import *
from idx_pipeline.utils.helper import random_sleep

from idx_pipeline.utils.helper import random_sleep

import logging 


LOGGER = logging.getLogger(__name__)


def llm_transalator(text: str) -> str: 
    generation_parser = JsonOutputParser(pydantic_object=PurposeTranslator)
    format_instructions = generation_parser.get_format_instructions()

    prompt_collections = PromptCollections()
    system_prompt = prompt_collections.get_system_purpose_prompt()
    user_prompt = prompt_collections.get_user_purpose_prompt()

    prompt = ChatPromptTemplate.from_messages([
        ("system", system_prompt),
        ('user', user_prompt )
    ])

    for model in ['gpt-oss-120b', 'gpt-oss-20b', 'gemini-2.5-flash', 'llama-3.3-70b']:
        try:
            llm = get_llm(model, temperature=0.4)
            LOGGER.info(f"LLM used for news: {model}")

            input_data = {
                'purpose': text, 
                'format_instructions': format_instructions
            }

            llm_chain = prompt | llm | generation_parser

            response = llm_chain.invoke(input_data)

            if response is None:
                LOGGER.warning("API call failed after all retries, trying next LLM...")
                continue

            if not response.get("purpose"):
                LOGGER.info("LLM translator returned incomplete result")
                continue
            
            return response.get('purpose')

        except Exception as error:
            LOGGER.warning(f"LLM failed with error: {error}")
            continue  

    LOGGER.error("All LLMs failed to return a valid translation")
    return None


def translator(text: str) -> str:   
    try: 

        translated = llm_transalator(text)
        random_sleep(1, 3)

        return translated 
    
    except Exception as error: 
        LOGGER.error(f"LLMTranslator failed: {error}. fallback with google translator") 
        translated =  GoogleTranslator(source='auto', target='en').translate(text) 
        random_sleep(1, 2)
        
        if not translated:
            return text
        
        return translated
    


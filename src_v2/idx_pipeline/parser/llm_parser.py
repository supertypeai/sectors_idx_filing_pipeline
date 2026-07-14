from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import JsonOutputParser

from idx_pipeline.llm.client import get_llm 
from idx_pipeline.llm.prompts import PromptCollections, FilingPayload
from idx_pipeline.utils.helper import write_json, open_json 
from idx_pipeline.parser.utils.helper import (
    normalize_company_name,
    normalize_holder_name,
    pop_classification,
    pop_purpose,
    to_kebab
)
from idx_pipeline.parser.core import (
    build_chained_filings,
    build_lookup_price_transaction,
    check_filing
)
from idx_pipeline.parser.utils.helper import compute_transactions

import time 
import fitz 
import logging 


LOGGER = logging.getLogger(__name__)

REQUIRED_FIELDS = (
    'company_name', 
    'holder_name', 
    'holding_before', 
    'holding_after',
    'price_transaction'
)


def get_texts(pdf_local_path: str) -> str:
    doc = fitz.open(pdf_local_path)
    
    text = ''
    
    for page in doc:
        text += page.get_text()

    return text 


def push_alert(alert: dict):
    existing_alerts = open_json('data_v2/alert/not_inserted.json') or []

    existing_alerts.append(alert)
    write_json(existing_alerts, 'data_v2/alert/not_inserted.json')


def enrich(
    result: dict,
    company_lookup: dict,
    pdf_url: str,
    filing_type: str = 'combine'
) -> dict:
    company_name = normalize_company_name(
        result.get('company_name').split(' - ')[-1]
    ).lower()

    company_by_name = {
        normalize_company_name(record['company_name']).lower(): record
        for record in company_lookup.values()
    }

    company_entry = company_by_name.get(company_name) or {}

    result['symbol'] = company_entry.get('symbol')
    result['company_name'] = normalize_company_name(
        company_entry.get('company_name') or company_name
    )
    result['holder_name'] = normalize_holder_name(
        result.get('holder_name').title()
    )
    result['source'] = pdf_url
    result['sector'] = to_kebab(company_entry.get('sector'))
    result['sub_sector'] = to_kebab(company_entry.get('sub_sector'))

    result['share_percentage_transaction'] = round(abs(
        (result.get('share_percentage_after') or 0.0) -
        (result.get('share_percentage_before') or 0.0)
    ), 3)

    transaction_computed = compute_transactions(
        price_transactions=result.get('price_transaction'),
        holding_after=result.get('holding_after'),
        holding_before=result.get('holding_before')
    )

    result['price'] = transaction_computed.get('price')
    result['transaction_value'] = transaction_computed.get('transaction_value')
    result['transaction_type'] = transaction_computed.get('transaction_type')
    result['net_shares_transacted'] = transaction_computed.get('net_shares_transacted')

    if filing_type == 'split':
        result['amount_transaction'] = sum(
            transaction.get('amount_transacted', 0)
            for transaction in result.get('price_transaction')
        )

    elif filing_type == 'combine':
        result['amount_transaction'] = abs(result.get('holding_before') - result.get('holding_after'))

    return result


def extract_with_llm(
    document_text: str,
    pdf_url: str,
    model_name: str = 'gpt-oss-120b',
    temperature: float = 0.3,
) -> dict | None:
    prompts = PromptCollections()

    generation_parser = JsonOutputParser(pydantic_object=FilingPayload)
    format_instructions = generation_parser.get_format_instructions()

    system_prompt = prompts.get_system_extraction_prompt()
    user_prompt = prompts.get_user_extraction_prompt()

    try:
        model = get_llm(
            model_name=model_name,
            temperature=temperature
        )

        input_data = {
            'document_text': document_text,
            'format_instructions': format_instructions
        }

        prompt = ChatPromptTemplate.from_messages([
            ("system", system_prompt),
            ('user', user_prompt)
        ])

        chain = prompt | model | generation_parser

        response = chain.invoke(input_data)

        LOGGER.info('LLM parser reasoning: %s', response['reasoning'])

        response.pop('reasoning')

        return response

    except Exception as error:
        LOGGER.error(
            'pdf url: %s error on llm parser error: %s', pdf_url, error, exc_info=True
        )
        return None


def parser_with_llm(
    pdf_local_path: str,
    pdf_url: str,
    company_lookup: dict,
    timestamp: str | None = None,
) -> tuple[list[dict], list[str]]:
    """
    Returns (filings, reasons). Reasons are empty on success. Alerting is the
    caller's job - this is already the last resort, so any reason here is final.
    """
    document_text = get_texts(pdf_local_path)

    response = extract_with_llm(
        document_text=document_text,
        pdf_url=pdf_url
    )

    if response is None:
        return [], ['llm parser call failed']

    # Guard if document is not an ownership report, unrelated attachment
    missing_fields = [
        field for field in REQUIRED_FIELDS
        if (
            response.get(field) is None
            or (
                field == 'price_transaction'
                and not response.get(field)
            )
        )
    ]

    if missing_fields:
        LOGGER.info('llm parser found no ownership data for %s', pdf_url)
        return [], [f"llm parser could not extract: {', '.join(missing_fields)}"]

    extracted_data = enrich(
        result=response,
        company_lookup=company_lookup,
        pdf_url=pdf_url
    )

    if not extracted_data.get('symbol'):
        LOGGER.error(
            'company %s not found in company_map for PDF: %s',
            extracted_data.get('company_name'), pdf_url
        )

        return [], [
            f"company '{extracted_data.get('company_name')}' might not in company_map.json"
        ]

    # run_ammend looks up the holder's previous filing by timestamp
    extracted_data['timestamp'] = timestamp

    reasons = check_filing(extracted_data, pdf_url)

    if reasons:
        return [], reasons

    price_data_list = build_lookup_price_transaction(
        extracted_data.get('price_transaction')
    )

    if len(price_data_list) > 1:
        results = build_chained_filings(
            price_data_list, extracted_data
        )

        if not results:
            return [], ['no valid transaction ordering reconciles the holdings']

    else:
        results = []

        _, transactions = next(iter(price_data_list.items()))
        purpose = transactions[0].get('purpose') if transactions else None

        pop_purpose(transactions)
        pop_classification(transactions)

        filing = {
            **extracted_data,
            'price_transaction': transactions,
            'purpose': purpose
        }

        enrich(
            filing,
            company_lookup,
            pdf_url,
            'split'
        )

        results.append(filing)

    time.sleep(2)

    return results, []


if __name__ == '__main__':
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
    )
        
    non_idx = 'pdf_test/non_idx/doc1.pdf'
    non_idx2 = 'pdf_test/non_idx/doc2.pdf'

    fail = 'data_v2/downloader/lk-13072026-7959-00.pdf'

    company_lookup = open_json('data_v2/idx_companies/company_map.json')

    result = parser_with_llm(
        pdf_local_path=fail, 
        pdf_url='test', 
        company_lookup=company_lookup
    )

    print(result)


# uv run -m idx_pipeline.parser.llm_parser
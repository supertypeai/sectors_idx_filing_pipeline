
import logging


LOGGER = logging.getLogger(__name__)


def check_transaction_mismatch(payload: dict) -> list[str]:
    holding_before = payload.get('holding_before', 0)
    holding_after = payload.get('holding_after', 0)
    net_shares = payload.get('net_shares_transacted', 0)
                    
    if holding_before is None or holding_after is None or not net_shares:
        return []

    expected_after = holding_before + net_shares

    if expected_after != holding_after:
        return [
            f"transaction value mismatch: holding_before={holding_before} + net_shares={net_shares} "
            f"= {expected_after}, but holding_after={holding_after}"
        ]

    return []


def check_missing_fields(payload: dict) -> list[str]:
    required_fields = (
        'symbol', 'holder_name', 'transaction_type', 'price_transaction',
        'holding_before', 'holding_after', 'transaction_value'
    )

    missing_fields = [
        field for field in required_fields
        if payload.get(field) is None or (isinstance(payload.get(field), str) and payload.get(field).strip().lower() in ('null', 'none', ''))
    ]

    if missing_fields:
        return [f"missing required fields: {', '.join(missing_fields)}"]

    return []


def filter_idx_filings(payload: dict) -> bool:
    reasons = check_missing_fields(payload) + check_transaction_mismatch(payload)

    if reasons:
        payload['reasons'] = reasons
        return True

    return False
    

def get_data_alert(payload: list[dict]) -> tuple[list[dict[str, any]], list[dict[str, any]]]:
    if not payload:
        LOGGER.info("No IDX filings data to filter.")
        return [], []

    data_insertable = []
    data_not_insertable = []

    for payload in payload: 
        if filter_idx_filings(payload):
            data_not_insertable.append(payload)

        else: 
            data_insertable.append(payload)
    
    LOGGER.info(f'Filtering completed. Insertable: {len(data_insertable)} | Not insertable: {len(data_not_insertable)}')
    return data_insertable, data_not_insertable



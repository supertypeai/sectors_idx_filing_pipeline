import logging 
import re


LOGGER = logging.getLogger(__name__)


KEYWORD_BUY = [
    "beli", "pembelian", "buy", "akumulasi", "investasi", "acquisition",
    "penambahan", "increase", "buyback", "buy back", "investment",
    "peningkatan", "akuisisi"
]
KEYWORD_SELL = [
    "jual", "penjualan", "sell", "divestasi", "divestment", "pengurangan",
    "reduksi", "disposal"
]
KEYWORD_TRANSFER = [
    "transfer", "pemindahan", "konversi", "conversion", "neutral",
    "tanpa perubahan", "alih", "pengalihan"
]
KEYWORD_INHERIT = ["waris", "inheritance", "hibah", "grant", "bequest"]
KEYWORD_MESOP = ["mesop", "msop", "esop", "program opsi saham", "employee stock option"]
KEYWORD_FREEFLOAT = ["free float", "free-float", "freefloat", "pemenuhan porsi publik"]
KEYWORD_RESTRUCTURING = ["restrukturisasi", "restructuring", "reorganisasi", "penyelesaian penurunan modal"]
KEYWORD_REPURCHASE = ['repo', 'transaksi repurchase', 'transaksi repo', 'repurchase agreement']
KEYWORD_PLACEMENT = ['penempatan saham revo', 'penempatan']

ORG_TOKENS = {
    "PT", "TBK", "PTE", "LTD", "LIMITED", "INC", "CORP", "CORPORATION",
    "NV", "BV", "B.V.", "GMBH", "LLC", "LP", "LLP", "PLC",
    "SDN BHD", "BHD", "BERHAD",
    "BANK", "SECURITIES", "SEKURITAS",
    "ASSET MANAGEMENT", "MANAJER INVESTASI", "INVESTMENT", "FUND",
    "YAYASAN", "FOUNDATION", "KOPERASI", "UNIVERSITAS", "PERSERO"
}

SLUG_PATTERN = re.compile(r"[^A-Za-z0-9]+")


def contains_any_keyword(text_lower: str, keywords: list[str]) -> bool:
    return any(keyword in text_lower for keyword in keywords)


def pop_purpose(transactions: list[dict[str, any]]):
    try:
        for transaction in transactions:
            transaction.pop('purpose', None)

    except Exception as error:
        LOGGER.error(f'Error pop_purpose: {error}')
        return []


def pop_classification(transactions: list[dict[str, any]]):
    try:
        for transaction in transactions:
            transaction.pop('classification', None)

    except Exception as error:
        LOGGER.error(f'Error pop_purpose: {error}')
        return []


def to_kebab(value: str | None) -> str:
    if not value:
        return "unknown"
    
    return SLUG_PATTERN.sub("-", value.strip()).strip("-").lower()


def crosses_50_percent_threshold(
    before_percentage: float,
    after_percentage: float
) -> bool:
    try:
        before_value = float(before_percentage)
        after_value = float(after_percentage)

    except (TypeError, ValueError):
        return False

    return (before_value < 50 <= after_value) or (before_value >= 50 > after_value)


def clean_number(num_str) -> int:
    if not num_str:
        return None
    
    clean_str = num_str.replace('.', '').replace(',', '.')

    try:
        return int(float(clean_str))
    
    except ValueError as error:
        LOGGER.error(f'clean number error: {error} {num_str}')
        return None


def clean_percentage(num_str) -> float:
    if not num_str: 
        return None
    
    clean_str = num_str.replace('%', '').strip().replace(',', '.')

    try:
        return round(float(clean_str), 3)
    
    except ValueError as error:
        LOGGER.error(f'clean percentage error: {error}')
        return None
    

def standardize_date(date_raw: str) -> str:
    try:
        month_map = {
            'Jan': '01', 'Feb': '02', 'Mar': '03', 'Apr': '04',
            'Mei': '05', 'Jun': '06', 'Jul': '07', 'Agu': '08',
            'Sep': '09', 'Okt': '10', 'Nov': '11', 'Des': '12'
        }

        parts = date_raw.split('-')
        
        if len(parts) == 3:
            day = parts[0].zfill(2)
            month = month_map.get(parts[1].strip(), '01')
            year = parts[2]
            date = f"{year}-{month}-{day}"

        else:
            date = date_raw 

        return date.strip()
    
    except Exception as error:
        LOGGER.error(f'standardize date error: {error}') 
        return None 


def map_transaction_type(type_raw: str) -> str:
    if not type_raw:
        return None
    
    type_lower = type_raw.lower()
    
    if 'penjualan' in type_lower:
        return 'sell'
    
    elif 'pembelian' in type_lower: 
        return 'buy'
    
    else:
        return 'others' 


def classify_transaction_type(type_raw: str, purpose: str) -> str | None: 
    if not type_raw:
        return None 
    
    type_lower = type_raw.lower()

    if purpose is not None: 
        purpose_lower = purpose.lower()

        if type_lower == 'lainnya': 
            buy_keywords = ['investasi', 'mesop', 'esop', 'pembelian']
            sell_keywords = ['divestasi', 'penjualan']
            
            if any(keyword in purpose_lower for keyword in buy_keywords): 
                return 'buy'
            
            if any(keyword in purpose_lower for keyword in sell_keywords): 
                return 'sell'
            
            return 'others'
            
        return map_transaction_type(type_raw)

    return map_transaction_type(type_raw)


def normalize_company_name(input_str: str) -> str:
    # Singaporean
    result = re.sub(r'\(?PTE\.?\)?,?\s*LTD\.?', '', input_str, flags=re.IGNORECASE)

    # International: Company Ltd / Co., Ltd / standalone Ltd / Limited
    result = re.sub(r'\bCOMPANY\s+LTD\.?|\bCO\.?,?\s*LTD\.?|\bLIMITED\b|\bLTD\.?', '', result, flags=re.IGNORECASE)

    # Malaysian
    result = re.sub(r'\bSDN\.?\s*BHD\.?|\bBHD\.?', '', result, flags=re.IGNORECASE)

    # Indonesian
    result = re.sub(
        r'\b(?:PT\.?|Pt\.?)\s*|\s*\(?(?:Tbk|tbk)\.?\)?[\s,]*|\s*\(Persero\)',
        '',
        result
    )

    result = re.sub(r'[\s,\.]+$', '', result)
    return re.sub(r'\s+', ' ', result).strip()


def normalize_holder_name(input_str: str) -> str:
    # Singaporean
    result = re.sub(r'\(?PTE\.?\)?,?\s*LTD\.?', '', input_str, flags=re.IGNORECASE)

    # International: Company Ltd / Co., Ltd / standalone Ltd / Limited
    result = re.sub(r'\bCOMPANY\s+LTD\.?|\bCO\.?,?\s*LTD\.?|\bLIMITED\b|\bLTD\.?', '', result, flags=re.IGNORECASE)

    # Malaysian
    result = re.sub(r'\bSDN\.?\s*BHD\.?|\bBHD\.?', '', result, flags=re.IGNORECASE)

    # Indonesian
    result = re.sub(
        r'\b(?:PT\.?|Pt\.?)\s*|\s*\(?(?:Tbk|tbk)\.?\)?[\s,]*|\s*\(Persero\)',
        '',
        result
    )

    # Academic and professional titles
    result = re.sub(r'\bDr\.?\s+', '', result, flags=re.IGNORECASE)
    result = re.sub(r'\s+S\.?\s*Kom\.?', '', result, flags=re.IGNORECASE)

    result = re.sub(r'[\s,\.]+$', '', result)
    return re.sub(r'\s+', ' ', result).strip()


def compute_transactions(
    price_transactions: list[dict[str, any]],
    holding_before: int | None = None,
    holding_after: int | None = None
) -> dict[str, any]:
    if not price_transactions:
        return {}

    total_buy_shares = 0
    total_buy_value = 0.0

    total_sell_shares = 0
    total_sell_value = 0.0

    total_others_shares = 0
    total_others_value = 0.0

    try:
        has_buy_sell = False

        for price_transaction in price_transactions:
            amount = int(price_transaction.get('amount_transacted') or 0)
            price = float(price_transaction.get('price') or 0.0)
            value = amount * price

            transaction_type = str(price_transaction.get('type')).lower()

            if transaction_type == 'buy':
                total_buy_shares += amount
                total_buy_value += value
                has_buy_sell = True

            elif transaction_type == 'sell':
                total_sell_shares += amount
                total_sell_value += value
                has_buy_sell = True

            else:
                total_others_shares += amount
                total_others_value += value

        if has_buy_sell:
            net_value = total_buy_value - total_sell_value
            net_shares = total_buy_shares - total_sell_shares

            if net_shares > 0:
                calculated_type = 'buy'

            elif net_shares < 0:
                calculated_type = 'sell'

            else:
                calculated_type = 'others'

            if net_shares != 0:
                weighted_average_price = abs(net_value / net_shares)

            else:
                weighted_average_price = 0.0

            return {
                "price": round(weighted_average_price, 3),
                "transaction_value": abs(int(net_value)),
                "transaction_type": calculated_type,
                "net_shares_transacted": net_shares
            }

        else:
            weighted_average_price = (
                total_others_value / total_others_shares
                if total_others_shares > 0
                else 0.0
            )

            signed_others_shares = total_others_shares

            if (
                holding_before is not None
                and holding_after is not None
            ):
                if holding_after < holding_before:
                    signed_others_shares = -total_others_shares

            return {
                "price": round(weighted_average_price, 3),
                "transaction_value": abs(int(total_others_value)),
                "transaction_type": "others",
                "net_shares_transacted": signed_others_shares
            }

    except Exception as error:
        LOGGER.error("Compute transaction error: %s", error, exc_info=True)
        return {}


def enrich_transaction(extracted_data: dict[str, any], filing_type: str = 'split'):
    try:
        holding_before = extracted_data.get('holding_before', 0)
        holding_after = extracted_data.get('holding_after', 0)

        # Compute top level transaction type, transaction value, price
        price_transaction = extracted_data.get('price_transaction', [])
        
        transaction_computed = compute_transactions(
            price_transaction,
            holding_before,
            holding_after
        )

        extracted_data['price'] = transaction_computed.get('price')
        extracted_data['transaction_value'] = transaction_computed.get('transaction_value')
        extracted_data['transaction_type'] = transaction_computed.get('transaction_type')
        extracted_data['net_shares_transacted'] = transaction_computed.get('net_shares_transacted')

        # Calculate amount transaction
        if filing_type == 'split':
            extracted_data['amount_transaction'] = sum(
                transaction.get('amount_transacted', 0)
                for transaction in price_transaction
            )

        elif filing_type == 'combine':
            extracted_data['amount_transaction'] = abs(holding_before - holding_after)

    except Exception as error:
        LOGGER.error(f'Error run_compute_transaction: {error}')
        return {}
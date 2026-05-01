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
    
    if 'koreksi atas laporan' in type_lower: 
        return type_lower
    elif 'pelaksanaan' in type_lower:
        return 'others'
    elif 'penjualan' in type_lower:
        return 'sell'
    elif 'pembelian' in type_lower: 
        return 'buy'
    elif 'lainnya' in type_lower: 
        return 'others'
    else:
        return None 


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


def clean_company_name(company_name: str) -> str:
    cleaned = company_name.strip()

    cleaned = re.sub(r'\bTbk\b(\s+\bTbk\b)+', 'Tbk', cleaned)

    cleaned = re.sub(r'\bTbk\.', 'Tbk', cleaned)
    
    if not cleaned.upper().startswith('PT'):
        cleaned = 'PT ' + cleaned

    return cleaned
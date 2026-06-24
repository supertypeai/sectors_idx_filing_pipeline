from collections import defaultdict
from itertools import permutations

from idx_pipeline.parser.utils.helper import (
    classify_transaction_type, 
    map_transaction_type,
    clean_number, 
    clean_percentage,
    standardize_date,
    normalize_company_name,
    normalize_holder_name,
    to_kebab, 
    pop_purpose,
    pop_classification
)
from idx_pipeline.utils.helper import write_json, open_json
from idx_pipeline.alerts.filter import filter_idx_filings

import fitz
import re
import logging 


LOGGER = logging.getLogger(__name__)
    

def extract_holder_name(text: str) -> str:
    try: 
        holder_name_pattern = r"Nama \(sesuai SID\)\s*:\s*(.+?)(?:\n|$)"

        holder_name = re.search(holder_name_pattern, text, re.IGNORECASE)
        holder_name = holder_name.group(1) if holder_name else None 
        
        if holder_name:
            holder_name = holder_name.title()
            # Convert any form of "pt" to "PT"
            holder_name = re.sub(r'\bPt\b', 'PT', holder_name)

        return holder_name
    
    except Exception as error: 
        LOGGER.error(f'extract holder name error: {error}')
        return None 


def extract_symbol_and_company_name(text: str) -> dict[str, str]:
    try: 
        # Company Name (with or without line breaks)
        pattern1 = r"Nama Perusahaan Tbk\s*:\s*([A-Z]+)\s*-\s*(.+?)(?=Tbk|PT|Jumlah Saham)"
        
        match = re.search(pattern1, text, re.DOTALL)
        
        if match:
            symbol = match.group(1).strip()
            company_name = match.group(2).strip()
            
            # Clean up company name: remove extra whitespace, newlines, and trailing commas
            company_name = re.sub(r'\s+', ' ', company_name) 
            company_name = company_name.rstrip(',').strip()   
            
            if 'Tbk' in text[match.end():match.end()+20]:
                company_name += ' Tbk'
            
            # LOGGER.info(f'Extracted symbol: {symbol}, company_name: {company_name}')
            symbol = f'{symbol}.JK'
            company_name = company_name

            return symbol, company_name
        
        return None, None 

    except Exception as error: 
        LOGGER.error(f'extract symbol and company name error: {error}')
        return None, None 


def extract_shares(text: str) -> dict[str, any]: 
    try:
        # Regex Patterns
        shares_before = r"Jumlah Saham Sebelum Transaksi\s*:\s*([\d\.,]+)"
        shares_after = r"Jumlah Saham Setelah Transaksi\s*:\s*([\d\.,]+)"
        
        # New Patterns for Voting Rights (handles optional % sign)
        vote_before = r"Hak Suara Sebelum Transaksi\s*:\s*([\d,]+)\s*%?"
        vote_after = r"Hak Suara Setelah Transaksi\s*:\s*([\d,]+)\s*%?"

        # Search
        shares_before = re.search(shares_before, text, re.IGNORECASE)
        shares_after = re.search(shares_after, text, re.IGNORECASE)
        vote_before = re.search(vote_before, text, re.IGNORECASE)
        vote_after = re.search(vote_after, text, re.IGNORECASE)

        shares_payload = {
            "holding_before": clean_number(shares_before.group(1)) if shares_before else None,
            "holding_after": clean_number(shares_after.group(1)) if shares_after else None,
            "share_percentage_before": clean_percentage(vote_before.group(1)) if vote_before else None,
            "share_percentage_after": clean_percentage(vote_after.group(1)) if vote_after else None
        }

        return shares_payload
    
    except Exception as error:
        LOGGER.error(f'extract shares error: {error}')
        return {} 


def extract_price_transaction(text: str) -> list[dict] | None:
    try:
        lines = [line.strip() for line in text.split('\n') if line.strip()]
      
        # Header Detection
        header_start_idx = None
        for index, line in enumerate(lines):
            if line == "Jenis" and index + 1 < len(lines) and lines[index + 1] == "Transaksi":
                header_start_idx = index
                break
        
        if header_start_idx is None:
            return None
        
        # Find Start of Data (After "Tujuan Transaksi")
        data_start_idx = None
        for index in range(header_start_idx, len(lines) - 1):
            if lines[index] == "Tujuan" and lines[index + 1] == "Transaksi":
                data_start_idx = index + 2
                break
        
        # Fallback for data start
        if data_start_idx is None:
             transaction_keywords = ["Penjualan", "Pembelian", "Lainnya", "Koreksi", 'Pelaksanaan', '(exercise)']
             for index in range(header_start_idx, len(lines)):
                 if lines[index] in transaction_keywords:
                     if lines[index] == "Pelaksanaan" and index + 1 < len(lines) and lines[index+1] in ["Jumlah", "Saham"]:
                         continue 
                     data_start_idx = index
                     break

        if data_start_idx is None:
            return None

        # Parse Transactions
        transactions = []
        index = data_start_idx
        
        transaction_keywords = [
            "Penjualan", "Pembelian", "Lainnya", 
            "Koreksi", 'Pelaksanaan', '(exercise)', 'Hibah'
        ]

        footer_keywords = [
            "Pemberi", "Keterangan", "Jika", 
            "Nama pemegang", "Informasi", "Saya bertanggung", "Hak Suara"
        ]

        while index < len(lines):
            line = lines[index]
            
            # If we hit a footer line, stop everything
            if any(line.startswith(k) for k in footer_keywords):
                break
            
            # Skip table headers
            if line == "Jenis" and index + 1 < len(lines) and lines[index + 1] == "Transaksi":
                while index < len(lines):
                    if lines[index] == "Tujuan" and index + 1 < len(lines) and lines[index + 1] == "Transaksi":
                        index += 2
                        break
                    index += 1
                continue
            
            if line in transaction_keywords:
                # A real transaction typically followed by "Tidak", "Ya", or "Langsung" 
                # before hitting a footer
                is_real_start = False
                # Look ahead 10 lines
                for i in range(1, 10): 
                    if index + i >= len(lines): break
                    val = lines[index + i]
                    if val in ["Tidak", "Ya", "Langsung"]:
                        is_real_start = True
                        break
                    if any(val.startswith(fk) for fk in footer_keywords):
                        break 
                
                # If it's not a real start (e.g., it's just the word "Penjualan" in the purpose),
                # skip this block and let the 'else' handle it or the previous purpose loop consume it
                if not is_real_start:
                    index += 1
                    continue

                # Parse Transaction Type 
                type_parts = [line]
                index += 1
                while index < len(lines):
                    curr = lines[index]
                    if curr in ["Tidak", "Ya"]:
                        break
                    if curr == "Jenis" or any(curr.startswith(k) for k in footer_keywords): 
                        break
                    type_parts.append(curr)
                    index += 1
                
                transaction_type = ' '.join(type_parts)
                
                if index < len(lines) and lines[index] in ["Tidak", "Ya"]: 
                    index += 1

                if index < len(lines) and lines[index] == "Langsung": 
                    index += 1

                # Find Amount (Anchor to "Saham" with validation)
                scan_limit = min(index + 100, len(lines))
                saham_found = False

                for saham_idx in range(index, scan_limit):
                    if lines[saham_idx] == "Saham":
                        if saham_idx > 0:
                            prev_line = lines[saham_idx - 1]
                            
                            if "," in prev_line and any(char.isdigit() for char in prev_line):
                                index = saham_idx - 1
                                saham_found = True
                                break

                if not saham_found:
                    index += 1
                    continue

                amount = lines[index] if index < len(lines) else None
                index += 1

                if index < len(lines) and lines[index] == "Saham":
                    index += 1

                # Collect Klasifikasi Saham
                classification_parts = ["Saham"]
                klasifikasi_scan_limit = min(index + 10, len(lines))

                for klasifikasi_idx in range(index, klasifikasi_scan_limit):
                    current_line = lines[klasifikasi_idx]

                    if "," in current_line and any(char.isdigit() for char in current_line):
                        index = klasifikasi_idx
                        break

                    if any(current_line.startswith(footer_keyword) for footer_keyword in footer_keywords):
                        index = klasifikasi_idx
                        break

                    if current_line in transaction_keywords:
                        index = klasifikasi_idx
                        break

                    # A bare dash is the null placeholder for Harga, never part of Klasifikasi Saham
                    if current_line == "-":
                        index = klasifikasi_idx
                        break

                    classification_parts.append(current_line)
                else:
                    index = klasifikasi_scan_limit

                classification_saham = " ".join(classification_parts)

                # Find Price
                scan_limit_price = min(index + 10, len(lines))
                price = None
                price_found = False

                for price_idx in range(index, scan_limit_price):
                    candidate = lines[price_idx]

                    if candidate == "-":
                        price = None
                        price_found = True
                        index = price_idx + 1
                        break

                    if "," in candidate and any(char.isdigit() for char in candidate):
                        price = candidate
                        price_found = True
                        index = price_idx + 1
                        break

                if not price_found:
                    price = lines[index] if index < len(lines) else None
                    index += 1
                
                # Find Date
                date_parts = []
                while index < len(lines):
                    part = lines[index]
                    # Check if this line starts a date
                    if re.match(r'^\d{1,2}[\s-]', part):
                        date_parts.append(part)
                        index += 1

                        # Collect remaining date parts
                        while index < len(lines):
                            part = lines[index]
                            date_parts.append(part)
                            index += 1
                            
                            if part.isdigit() and len(part) == 4: 
                                break
                            if len(date_parts) >= 5: 
                                break
                        break

                    index += 1
                    if len(date_parts) > 0 or index >= min(len(lines), index + 10):
                        break

                date = ' '.join(date_parts) if date_parts else None
                
                # Find Purpose (always exactly one line after date)
                purpose_parts = []
                while index < len(lines):
                    curr = lines[index]
                    
                    # Stop if footer
                    if any(curr.startswith(k) for k in footer_keywords): 
                        break
                    
                    # Stop if table header
                    if curr == "Jenis" and index + 1 < len(lines) and lines[index + 1] == "Transaksi":
                        break

                    # Check if NEXT line is start of new transaction (look ahead)
                    if index + 1 < len(lines) and lines[index + 1] in transaction_keywords:
                        # Verify next line is real transaction start
                        is_next_real_start = False
                        for i in range(2, 12):  # Look from index+2 onwards
                            if index + i >= len(lines): break
                            val = lines[index + i]
                            if val in ["Tidak", "Ya", "Langsung"]:
                                is_next_real_start = True
                                break
                            if any(val.startswith(fk) for fk in footer_keywords):
                                break
                        
                        if is_next_real_start:
                            # Next line starts new transaction, current line is last part of purpose
                            purpose_parts.append(curr)
                            index += 1
                            break
                    
                    # Current line is part of purpose
                    purpose_parts.append(curr)
                    index += 1

                purpose = ' '.join(purpose_parts)

                LOGGER.info(
                    f"DEBUG: transaction_type='{transaction_type}', amount={amount}, price={price}, date={date}"
                )

                # Build Object
                # print(f'raw tx type: {transaction_type} | purpose: {purpose}')

                type_mapped = map_transaction_type(transaction_type)
                amount_clean = clean_number(amount) 
                price_clean = clean_number(price) 
                date_clean = standardize_date(date) 

                transaction = {
                    "type": type_mapped,
                    "amount_transacted": amount_clean,
                    "price": price_clean,
                    "date": date_clean,
                    "purpose": purpose,
                    "classification": classification_saham
                }

                transactions.append(transaction)

            else:
                index += 1
      
        if not transactions:
            return None

        return transactions 
    
    except Exception as error:
        LOGGER.error(f'extract price transaction error: {error}', exc_info=True)
        return None
    

def build_lookup_price_transaction(transactions: list[dict[str, any]]):
    try: 
        transaction_lookup = defaultdict(list)

        for transaction in transactions:
            transaction_type = transaction.get('type')
            transaction_lookup[transaction_type].append(transaction)

        return transaction_lookup

    except Exception as error:
        LOGGER.error(f'Error split_price_transaction: {error}')
        return {}


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
    

def detect_transaction_tables(doc) -> dict:
    keys = ['jenis transaksi', 'klasifikasi saham']
    pages_with_tables = []
    
    for page_num, page in enumerate(doc, start=0):
        text = page.get_text().lower()
        # Normalize all whitespace to single spaces
        text = re.sub(r'\s+', ' ', text)
        
        if all(key in text for key in keys):
            pages_with_tables.append(page_num)
    
    return {
        'count': len(pages_with_tables),
        'pages': pages_with_tables
    }


def collect_extract_shares(doc: fitz.Document, pdf_url: str) -> dict | None:
    extracted_data = {}

    for page_index in [0, 1]:
        if page_index >= len(doc):
            break

        text = doc[page_index].get_text()
        shares_data = extract_shares(text)

        for key, value in shares_data.items():
            if value is not None:
                extracted_data[key] = value

        share_before = extracted_data.get('holding_before')
        share_after = extracted_data.get('holding_after')

        if share_before is not None and share_after is not None:
            if share_before == share_after:
                LOGGER.info(f"skipping {pdf_url}: shares unchanged")
                return None

    # LOGGER.info(f"extracted shares: {extracted_data}\n")

    share_percentage_transaction = round(abs(
        (extracted_data.get('share_percentage_after') or 0.0) -
        (extracted_data.get('share_percentage_before') or 0.0)
    ), 3)
    extracted_data['share_percentage_transaction'] = share_percentage_transaction

    return extracted_data


def enrich_payload(
    doc: fitz.Document,
    extracted_data: dict,
    company_lookup: dict,
    pdf_url: str
) -> bool:
    text = doc[0].get_text()

    holder_name = extract_holder_name(text)
    symbol, company_name = extract_symbol_and_company_name(text)

    if not symbol:
        existing_alerts = open_json('data_v2/alert/not_inserted.json') or []
        existing_alerts.append({
            'date': '-',
            'reasons': ['failed to extract symbol from PDF text'],
            'source': pdf_url,
            'symbol': '-'
        })

        write_json(existing_alerts, 'data_v2/alert/not_inserted.json')
        LOGGER.error(f"failed to extract symbol for PDF: {pdf_url}")
        return False

    company_entry = company_lookup.get(symbol)

    if company_entry:
        company_name = company_entry.get('company_name')

    sector = company_entry.get('sector')
    sub_sector = company_entry.get('sub_sector')

    extracted_data['symbol'] = symbol.upper()
    extracted_data['company_name'] = normalize_company_name(company_name)
    extracted_data['holder_name'] = normalize_holder_name(holder_name)
    extracted_data['source'] = pdf_url
    extracted_data['sector'] = to_kebab(sector)
    extracted_data['sub_sector'] = to_kebab(sub_sector)

    return True


def extract_prices(doc: fitz.Document):
    detected_pages = detect_transaction_tables(doc=doc)
    pages_index = detected_pages.get('pages')

    full_text_lines = [
        doc[page_index].get_text()
        for page_index in range(pages_index[0], pages_index[-1] + 1)
    ]
    combined_text = "\n".join(full_text_lines)
   
    price_transactions =  extract_price_transaction(combined_text)

    return price_transactions


def compute_intermediate_share_percentage(
    intermediate_holding: int,
    pdf_holding_before: int,
    pdf_share_percentage_before: float
) -> float:
    if pdf_share_percentage_before == 0 or pdf_holding_before == 0:
        return 0.0
    
    total_shares = pdf_holding_before / (pdf_share_percentage_before / 100)
    return round(intermediate_holding / total_shares * 100, 3)


def find_valid_ordering(
    type_signed_amounts: dict[str, int],
    holding_before: int,
    holding_after: int
) -> list[str] | None:
    transaction_types = list(type_signed_amounts.keys())

    for ordering in permutations(transaction_types):
        current_holding = holding_before
        valid = True

        for transaction_type in ordering:
            current_holding += type_signed_amounts[transaction_type]

            if current_holding < 0:
                valid = False
                break

        if valid and current_holding == holding_after:
            return list(ordering)

    return None


def build_chained_filings(
    price_data_list: dict[str, list[dict]],
    extracted_data: dict
) -> list[dict]:
    pdf_holding_before = extracted_data.get('holding_before', 0)
    pdf_holding_after = extracted_data.get('holding_after', 0)
    pdf_share_percentage_before = extracted_data.get('share_percentage_before', 0.0)
    pdf_share_percentage_after = extracted_data.get('share_percentage_after', 0.0)

    type_amounts = {
        transaction_type: sum(
            transaction.get('amount_transacted', 0)
            for transaction in transactions
        )
        for transaction_type, transactions in price_data_list.items()
    }

    # Derive signed amounts - others direction is computed from net
    buy_total = type_amounts.get('buy', 0)
    sell_total = type_amounts.get('sell', 0)
    net_holding_change = pdf_holding_after - pdf_holding_before
    others_effect = net_holding_change - buy_total + sell_total

    type_signed_amounts = {}
    if 'buy' in type_amounts:
        type_signed_amounts['buy'] = buy_total

    if 'sell' in type_amounts:
        type_signed_amounts['sell'] = -sell_total

    if 'others' in type_amounts:
        type_signed_amounts['others'] = others_effect

    valid_ordering = find_valid_ordering(
        type_signed_amounts,
        pdf_holding_before,
        pdf_holding_after
    )

    if valid_ordering is None:
        LOGGER.error(f"No valid transaction ordering found for source: {extracted_data.get('source')}")
        return []

    results = []
    current_holding = pdf_holding_before
    current_share_percentage = pdf_share_percentage_before

    for index, transaction_type in enumerate(valid_ordering):
        is_last_filing = index == len(valid_ordering) - 1
        transactions = price_data_list[transaction_type]

        purpose = transactions[0].get('purpose') if transactions else None
        pop_purpose(transactions)

        filing_holding_before = current_holding
        filing_share_percentage_before = current_share_percentage

        next_holding = current_holding + type_signed_amounts[transaction_type]

        if is_last_filing:
            filing_holding_after = pdf_holding_after
            filing_share_percentage_after = pdf_share_percentage_after

        else:
            filing_holding_after = next_holding
            filing_share_percentage_after = compute_intermediate_share_percentage(
                next_holding,
                pdf_holding_before,
                pdf_share_percentage_before
            )

        filing = {
            **extracted_data,
            'price_transaction': transactions,
            'purpose': purpose,
            'holding_before': filing_holding_before,
            'holding_after': filing_holding_after,
            'share_percentage_before': filing_share_percentage_before,
            'share_percentage_after': filing_share_percentage_after,
            'share_percentage_transaction': round(
                abs(filing_share_percentage_after - filing_share_percentage_before), 3
            )
        }

        enrich_transaction(filing, 'split')
        results.append(filing)

        current_holding = next_holding
        current_share_percentage = filing_share_percentage_after

    return results


def parse_document(
    doc: fitz.Document,
    pdf_url: str,
    company_lookup: dict,
) -> list[dict]:
    extracted_data = collect_extract_shares(doc, pdf_url)

    if extracted_data is None:
        return []

    if not enrich_payload(doc, extracted_data, company_lookup, pdf_url):
        return []

    price_transactions = extract_prices(doc)
    combined_filing = {**extracted_data, 'price_transaction': price_transactions}
    enrich_transaction(combined_filing, 'combine')

    if filter_idx_filings(combined_filing):
        existing_alerts = open_json('data_v2/alert/not_inserted.json') or []
        existing_alerts.append(combined_filing)

        write_json(existing_alerts, 'data_v2/alert/not_inserted.json')
        return []

    price_data_list = build_lookup_price_transaction(price_transactions)
    
    if len(price_data_list) > 1:
        results = build_chained_filings(price_data_list, extracted_data)
    
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

        enrich_transaction(filing, 'split')
        results.append(filing)

    return results


def parser_new_document(
    pdf_local_path: str,
    pdf_url: str,
    company_lookup: dict,
) -> list[dict]:
    doc = fitz.open(pdf_local_path)

    try:
        result = parse_document(
            doc, 
            pdf_url, 
            company_lookup
        )

    finally:
        doc.close()

    return result


if __name__ == '__main__': 
    result = parser_new_document('test_pdf.pdf')
    # print(result)


# uv run -m idx_pipeline.parser.core
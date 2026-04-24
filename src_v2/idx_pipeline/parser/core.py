from idx_pipeline.parser.utils.helper import (
    classify_transaction_type, 
    clean_number, 
    clean_percentage,
    standardize_date,
    clean_company_name
)

import fitz
import re
import copy 
import logging 


LOGGER = logging.getLogger(__name__)
    
SLUG_PATTERN = re.compile(r"[^A-Za-z0-9]+")


def to_kebab(value: str | None) -> str:
    if not value:
        return "unknown"
    
    return SLUG_PATTERN.sub("-", value.strip()).strip("-").lower()


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


def extract_price_transaction(text: str) -> tuple[dict[str, any] | None, dict[str, any]]:
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
            "Koreksi", 'Pelaksanaan', '(exercise)'
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

                for i in range(index, scan_limit):
                    if lines[i] == "Saham":
                        # Verify line before "Saham" is a valid amount, 
                        # to handle page splitted
                        if i > 0:
                            prev_line = lines[i - 1]
                            # Amount must have comma and digits
                            if ',' in prev_line and any(c.isdigit() for c in prev_line):
                                # Valid Saham, amount is line before it
                                index = i - 1
                                saham_found = True
                                break
                        # Otherwise, this is orphaned "Saham", keep searching

                if not saham_found:
                    # Fallback: skip to next transaction
                    index += 1
                    continue

                amount = lines[index] if index < len(lines) else None  # Extract amount
                index += 1  # Move to "Saham"

                if index < len(lines) and lines[index] == "Saham": 
                    index += 1
                
                # Find Price
                # The item immediately before the date is the Price.
                scan_limit_price = min(index + 10, len(lines))
                price = None

                for i in range(index, scan_limit_price):
                    line = lines[i]
                    # Price pattern: contains comma and has digits (e.g., "29,00", "121,00")
                    if ',' in line and any(c.isdigit() for c in line):
                        price = line
                        index = i + 1
                        break

                if price is None:
                    # Fallback
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

                # LOGGER.info(f"DEBUG: transaction_type='{transaction_type}', amount={amount}, price={price}, date={date}")

                # Build Object
                # print(f'raw tx type: {transaction_type} | purpose: {purpose}')

                type_mapped = classify_transaction_type(transaction_type, purpose)
                amount_clean = clean_number(amount) 
                price_clean = clean_number(price) 
                date_clean = standardize_date(date) 

                transaction = {
                    "type": type_mapped,
                    "amount_transacted": amount_clean,
                    "price": price_clean,
                    "date": date_clean,
                    "purpose": purpose
                }
                transactions.append(transaction)
            else:
                index += 1

        if not transactions:
            return None

        # LOGGER.info(f'raw transaction: {transactions}')
        
        result_others, result_no_others = split_price_transaction(transactions)
        
        return result_others, result_no_others 
    
    except Exception as error:
        LOGGER.error(f'extract price transaction error: {error}')
        return None


def pop_purpose(transactions: list[dict[str, any]]):
    try:
        for transaction in transactions:
            transaction.pop('purpose', None)

    except Exception as error:
        LOGGER.error(f'Error pop_purpose: {error}')
        return []


def split_price_transaction(transactions: list[dict[str, any]]) -> tuple[dict[str, any] | None, dict[str, any]]:
    try: 
        result_no_others_list = []
        result_others_list = []

        result_no_others_dict = {}
        result_others_dict = {}

        for transaction in transactions: 
            type = transaction.get('type')

            if type == 'others':
                result_others_list.append(transaction)
            elif type in ('sell', 'buy'):
                result_no_others_list.append(transaction)

        if result_no_others_list:
            result_no_others_dict.update({
                'price_transaction': result_no_others_list,
                'purpose': result_no_others_list[-1].get('purpose')
            })
            pop_purpose(result_no_others_list)

        if result_others_list:
            result_others_dict.update({
                'price_transaction': result_others_list,
                'purpose': result_others_list[-1].get('purpose')
            })
            pop_purpose(result_others_list)

        return result_others_dict if result_others_dict else None, result_no_others_dict if result_no_others_dict else None

    except Exception as error:
        LOGGER.error(f'Error split_price_transaction: {error}')
        return {}, {} 


def compute_transactions(price_transactions: list[dict[str, any]]) -> dict[str, any]:
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
            if total_others_shares > 0:
                weighted_average_price = total_others_value / total_others_shares

            else:
                weighted_average_price = 0.0
            
            return {
                "price": round(weighted_average_price, 3),
                "transaction_value": abs(int(total_others_value)),
                "transaction_type": "others",
                "net_shares_transacted": total_others_shares
            }

    except Exception as error:
        LOGGER.error(f"Compute transaction error: {error}")
        return {}


def enrich_transaction(extracted_data: dict[str, any]):
    try:
        # Compute top level transaction type, transaction value, price
        transaction_computed = compute_transactions(extracted_data.get('price_transaction'))

        extracted_data['price'] = transaction_computed.get('price')
        extracted_data['transaction_value'] = transaction_computed.get('transaction_value')
        extracted_data['transaction_type'] = transaction_computed.get('transaction_type')
        extracted_data['net_shares_transacted'] = transaction_computed.get('net_shares_transacted')

        # Calculate amount transaction
        holding_before = extracted_data.get('holding_before', 0)
        holding_after = extracted_data.get('holding_after', 0)
        
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
) -> None:
    text = doc[0].get_text()

    holder_name = extract_holder_name(text)
    symbol, company_name = extract_symbol_and_company_name(text)

    if company_lookup and symbol:
        company_entry = company_lookup.get(symbol)

        if company_entry:
            company_name = company_entry.get('company_name')

        sector = company_entry.get('sector')
        sub_sector = company_entry.get('sub_sector')

    extracted_data['symbol'] = symbol.upper()
    extracted_data['company_name'] = clean_company_name(company_name)
    extracted_data['holder_name'] = holder_name
    extracted_data['source'] = pdf_url
    extracted_data['sector'] = to_kebab(sector)
    extracted_data['sub_sector'] = to_kebab(sub_sector)


def extract_prices(doc: fitz.Document) -> tuple[dict | None, dict | None]:
    detected_pages = detect_transaction_tables(doc=doc)
    pages_index = detected_pages.get('pages')

    full_text_lines = [
        doc[page_index].get_text()
        for page_index in range(pages_index[0], pages_index[-1] + 1)
    ]
    combined_text = "\n".join(full_text_lines)

    return extract_price_transaction(combined_text)


def parse_document(
    doc: fitz.Document,
    pdf_url: str,
    company_lookup: dict,
) -> tuple[dict | None, dict | None]:
    results = []

    extracted_data = collect_extract_shares(doc, pdf_url)

    if extracted_data is None:
        return []

    enrich_payload(
        doc, 
        extracted_data, 
        company_lookup,
        pdf_url
    )

    price_data_others, price_data_no_others = extract_prices(doc)

    if price_data_others is not None:
        extracted_data_others = copy.deepcopy(extracted_data)
        extracted_data_others.update(price_data_others)
        enrich_transaction(extracted_data_others)

        extracted_data_others['split_variant'] = 'others'
        results.append(extracted_data_others)

    if price_data_no_others is not None:
        extracted_data.update(price_data_no_others)
        enrich_transaction(extracted_data)

        extracted_data['split_variant'] = 'primary'
        results.append(extracted_data)

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
    print(result)


# uv run -m idx_pipeline.parser.parser_idx_new_copy
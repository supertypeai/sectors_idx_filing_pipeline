from src.common.log import get_logger

import fitz
import re
import json 
import copy 


LOGGER = get_logger(__name__)

def open_json(filepath: str) -> dict | None:
    try:
        with open(filepath, 'r', encoding='utf-8') as file:
            data = json.load(file)
            return data
    
    except Exception as error:
        LOGGER.error(f'Error opening JSON file {filepath}: {error}')
        return None


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

    
def extract_holder_name(text: str) -> dict[str, str]:
    try: 
        holder_name_pattern = r"Nama \(sesuai SID\)\s*:\s*(.+?)(?:\n|$)"

        holder_name = re.search(holder_name_pattern, text, re.IGNORECASE)
        holder_name = holder_name.group(1) if holder_name else None 
        
        if holder_name:
            holder_name = holder_name.title()
            # Convert any form of "pt" to "PT"
            holder_name = re.sub(r'\bPt\b', 'PT', holder_name)

        holder_name = {'holder_name': holder_name}
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
            
            LOGGER.info(f'\nExtracted symbol: {symbol}, company_name: {company_name}')
            return {
                'symbol': f'{symbol}.JK',
                'company_name': company_name
            }
        
        return {'symbol': None, 'company_name': None}

    except Exception as error: 
        LOGGER.error(f'extract symbol and company name error: {error}')
        return {'symbol': None, 'company_name': None}


def extract_shares(text: str) -> dict[str, any]: 
    try:
        # Regex Patterns
        shares_before = r"Jumlah Saham Sebelum Transaksi\s*:\s*([\d\.,]+)"
        shares_after  = r"Jumlah Saham Setelah Transaksi\s*:\s*([\d\.,]+)"
        
        # New Patterns for Voting Rights (handles optional % sign)
        vote_before   = r"Hak Suara Sebelum Transaksi\s*:\s*([\d,]+)\s*%?"
        vote_after    = r"Hak Suara Setelah Transaksi\s*:\s*([\d,]+)\s*%?"

        # Search
        shares_before = re.search(shares_before, text, re.IGNORECASE)
        shares_after  = re.search(shares_after, text, re.IGNORECASE)
        vote_before   = re.search(vote_before, text, re.IGNORECASE)
        vote_after    = re.search(vote_after, text, re.IGNORECASE)

        shares_payload = {
            "holding_before": clean_number(shares_before.group(1)) if shares_before else None,
            "holding_after":  clean_number(shares_after.group(1)) if shares_after else None,
            "share_percentage_before": clean_percentage(vote_before.group(1)) if vote_before else None,
            "share_percentage_after":  clean_percentage(vote_after.group(1)) if vote_after else None
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
            
            # If hit a footer line, stop everything
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
                # A real transaction must be followed by "Tidak", "Ya", or "Langsung" 
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
                
                # If it's not a real start (e.g., it's just the word "Penjualan" in the purpose)
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

                # Find Amount 
                scan_limit = min(index + 100, len(lines))
                saham_found = False

                for i in range(index, scan_limit):
                    if lines[i] == "Saham":
                        # Amount is the line immediately before "Saham"
                        index = i - 1
                        saham_found = True
                        break

                if not saham_found:
                    # Fallback: skip to next transaction
                    index += 1
                    continue

                amount = lines[index] if index < len(lines) else None
                index += 1  

                if index < len(lines) and lines[index] == "Saham": 
                    index += 1
                
                # Find Price
                date_start_index = -1
                scan_limit_date = min(index + 10, len(lines))
                
                for k in range(index, scan_limit_date):
                    val = lines[k]
                    # Regex to find Date start
                    if re.match(r'^\d{1,2}\s?-$', val): 
                        date_start_index = k
                        break
                
                if date_start_index != -1 and date_start_index > index:
                    # Found the date, The line before it is the Price
                    price = lines[date_start_index - 1]
                    index = date_start_index 
                    
                else:
                    # Fallback if Regex fails (assume standard "Biasa" structure)
                    if index < len(lines) and lines[index] == "Biasa": 
                        index += 1

                    price = lines[index] if index < len(lines) else None
                    index += 1
                
                # Find Date
                date_parts = []
                while index < len(lines):
                    part = lines[index]
                    date_parts.append(part)
                    index += 1
                    if part.isdigit() and len(part) == 4: 
                        break
                    if len(date_parts) >= 5: 
                        break
                
                date = ' '.join(date_parts)

                # Find Purpose
                purpose_parts = []
                while index < len(lines):
                    curr = lines[index]
                    
                    # Stop if footer
                    if any(curr.startswith(k) for k in footer_keywords): 
                        break
                    
                    # Stop if table header
                    if curr == "Jenis" and index + 1 < len(lines) and lines[index + 1] == "Transaksi":
                        break

                    # Check if next line is start of new transaction 
                    if index + 1 < len(lines) and lines[index + 1] in transaction_keywords:
                        # Verify next line is real transaction start
                        is_next_real_start = False
                        # Look from index+2 onwards
                        for i in range(2, 12):  
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

                LOGGER.info(f"DEBUG: transaction_type='{transaction_type}', amount={amount}, price={price}, date={date}")

                # Build Object
                type_mapped = map_transaction_type(transaction_type)
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

        LOGGER.info(f'\nraw transaction: {transactions}\n')
        
        result_others, result_no_others = split_price_transaction(transactions)
        
        return result_others, result_no_others 
    
    except Exception as error:
        LOGGER.error(f'Error extract_price_transaction: {error}')
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
            
            type = str(price_transaction.get('type')).lower()

            if type =='buy': 
                total_buy_shares += amount
                total_buy_value += value
                has_buy_sell = True 
            elif type == 'sell':
                total_sell_shares += amount
                total_sell_value += value
                has_buy_sell = True 
            else:
                total_others_shares += amount
                total_others_value += value

        if has_buy_sell:
            # Net transaction value (Buy – Sell)
            net_value = total_buy_value - total_sell_value
            
            # Net transacted share amount (Buy-Sell)
            net_shares = total_buy_shares - total_sell_shares

            if net_value > 0:
                type = 'buy'
            elif net_value < 0:
                type = 'sell'
            else:
                type = 'others'

            if net_shares != 0:
                # We use abs() because price cannot be negative
                w_avg_price = abs(net_value / net_shares)
            else:
                w_avg_price = 0.0

            return {
                "price": round(w_avg_price, 3),
                "transaction_value": abs(int(net_value)),
                "transaction_type": type
            }
        
        else:
            # Calculate Price (Total Value / Total Shares)
            if total_others_shares > 0:
                w_avg_price = total_others_value / total_others_shares
            else:
                w_avg_price = 0.0
            
            return {
                "price": round(w_avg_price, 3),
                "transaction_value": abs(int(total_others_value)),
                "transaction_type": "others"
            }

    except Exception as error:
        LOGGER.error(f'compute transaction error: {error}')
        return {}


def run_compute_transaction(extracted_datas: dict[str, any], filename: str):
    try:
        # Compute top level transaction type, transaction value, price
        transaction_computed = compute_transactions(extracted_datas.get('price_transaction'))
        extracted_datas.update({'price': transaction_computed.get('price')})
        extracted_datas.update({'transaction_value': transaction_computed.get('transaction_value')})
        extracted_datas.update({'transaction_type': transaction_computed.get('transaction_type')})
    
        # Calculate amount transaction
        amount_transaction = abs(extracted_datas.get('holding_before') - extracted_datas.get('holding_after'))
        extracted_datas.update({'amount_transaction': amount_transaction})

        extracted_datas.update({'source': filename}) 
    
    except Exception as error:
        LOGGER.error(f'Error run_compute_transaction: {error}')
        return {}
    

def detect_transaction_tables(pdf_path: str) -> dict:
    doc = fitz.open(pdf_path)
    keys = ['jenis transaksi', 'klasifikasi saham']
    pages_with_tables = []
    
    for page_num, page in enumerate(doc, start=0):
        text = page.get_text().lower()
        # Normalize all whitespace to single spaces
        text = re.sub(r'\s+', ' ', text)
        
        if all(key in text for key in keys):
            pages_with_tables.append(page_num)
    
    doc.close()
    
    return {
        'count': len(pages_with_tables),
        'pages': pages_with_tables
    }


def parser_new_document(filename: str): 
    doc = fitz.open(filename)

    extracted_data = {}

    # Extract shares
    for page_index in [0,1]:
        if page_index > len(doc):
            break 

        text = doc[page_index].get_text()

        shares_data =  extract_shares(text)
        
        for key, value in shares_data.items():
            if value is not None:
                extracted_data[key] = value

        share_before = extracted_data.get('holding_before')
        share_after = extracted_data.get('holding_after')

        if share_before is not None and share_after is not None:
            if share_before == share_after:
                LOGGER.info(f"Skipping {filename}: Shares unchanged.")
                return None
            
    LOGGER.info(f'extracted_data_shares: {extracted_data}\n')

    company_lookup = open_json('data/company/company_map.json')

    # Calculate after get all shares data (some data splitted into next page)
    share_percentage_transaction = round(abs(
        (extracted_data.get("share_percentage_after") or 0.0) - (extracted_data.get("share_percentage_before") or 0.0)
    ), 3) 
    extracted_data.update({'share_percentage_transaction': share_percentage_transaction})

    # Extract holder name and symbol 
    page = doc[0]
    text = page.get_text()
    holder_name = extract_holder_name(text)

    symbol = extract_symbol_and_company_name(text)
    
    # Cross verify symbol with company lookup
    if company_lookup and symbol: 
        company_name_lookup = company_lookup.get(symbol.get('symbol'))
        if company_name_lookup:
            symbol['company_name'] = company_name_lookup.get('company_name')

    extracted_data.update(symbol)
    extracted_data.update(holder_name)

    LOGGER.info(f'\nextracted_data holder and symbol: {extracted_data}\n')

    # Extract price transaction
    detected_pages = detect_transaction_tables(filename)
    pages_index = detected_pages.get('pages')

    full_text_lines = []
    for page_index in range(pages_index[0], pages_index[-1] + 1):
            page = doc[page_index]
            full_text_lines.append(page.get_text())
            
    combined_text = "\n".join(full_text_lines)

    if "price_transaction" not in extracted_data:
        price_data_others, price_data_no_others = extract_price_transaction(combined_text)
       
        if price_data_others is not None:
            extracted_data_others = copy.deepcopy(extracted_data)
            extracted_data_others.update(price_data_others)
            

        if price_data_no_others is not None:
            extracted_data.update(price_data_no_others) 

    if price_data_no_others is not None:
        run_compute_transaction(extracted_data, filename)
    if price_data_others is not None:
        run_compute_transaction(extracted_data_others, filename)

    return extracted_data_others if price_data_others else None, extracted_data if price_data_no_others else None
  

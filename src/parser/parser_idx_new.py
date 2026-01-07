from src.common.log import get_logger

import fitz
import re
import json 

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


def extract_price_transaction(text: str) -> dict[str, any]:
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
        
        transaction_keywords = ["Penjualan", "Pembelian", "Lainnya", "Koreksi", 'Pelaksanaan', '(exercise)']
        footer_keywords = ["Pemberi", "Keterangan", "Jika", "Nama pemegang", "Informasi", "Saya bertanggung", "Hak Suara"]

        while index < len(lines):
            line = lines[index]
            
            # If we hit a footer line, stop everything.
            if any(line.startswith(k) for k in footer_keywords):
                break

            if line in transaction_keywords:
                # A real transaction must be followed by "Tidak", "Ya", or "Langsung" 
                # before hitting a footer.
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
                # skip this block and let the 'else' handle it or the previous purpose loop consume it.
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

                    # Don't break on keywords here, or we break multi-word types.
                    # Instead check if we are hitting the ownership field.
                    if any(curr.startswith(k) for k in footer_keywords): 
                        break

                    type_parts.append(curr)
                    index += 1
                
                transaction_type = ' '.join(type_parts)
                
                if index < len(lines) and lines[index] in ["Tidak", "Ya"]: 
                    index += 1

                if index < len(lines) and lines[index] == "Langsung": 
                    index += 1

                # Find Amount (Anchor to "Saham") 
                scan_limit = min(index + 15, len(lines))
                for i in range(index, scan_limit):
                    if lines[i] == "Saham":
                        index = i - 1
                        break
                
                amount = lines[index] if index < len(lines) else None
                index += 1 # At Saham

                if index < len(lines) and lines[index] == "Saham": 
                    index += 1
                
                # Find Price
                # The item immediately before the date is the Price.
                date_start_index = -1
                scan_limit_date = min(index + 10, len(lines))
                
                for k in range(index, scan_limit_date):
                    val = lines[k]
                    # Regex to find Date start: 1 or 2 digits followed by hyphen (e.g., "01-", "24-")
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

                    # Stop if NEW Transaction, but only if it's a REAL one
                    if curr in transaction_keywords:
                        is_next_real_start = False
                        for i in range(1, 10):
                            if index + i >= len(lines): break
                            val = lines[index + i]
                            if val in ["Tidak", "Ya", "Langsung"]:
                                is_next_real_start = True
                                break
                            if any(val.startswith(fk) for fk in footer_keywords):
                                break
                        
                        if is_next_real_start:
                            break
                        # If not a real start, treat this keyword as normal text (part of purpose)

                    purpose_parts.append(curr)
                    index += 1
                
                purpose = ' '.join(purpose_parts)

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

        result = {
            "price_transaction": transactions,
            "purpose": transactions[0]["purpose"] if transactions else None
        }
        for transaction in transactions:
            transaction.pop('purpose', None)

        return result
        
    except Exception as error:
        LOGGER.error(f'extract price transaction error: {error}')
        return None


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
    full_text_lines = []
    for page_index in range(min(len(doc), 5)):
            page = doc[page_index]
            full_text_lines.append(page.get_text())
            
    combined_text = "\n".join(full_text_lines)

    if "price_transaction" not in extracted_data:
        price_data = extract_price_transaction(combined_text)
        if price_data:
            extracted_data.update(price_data)
            LOGGER.info(f"Found price transaction on page {page_index + 1}\n")
    
    # Compute top level transaction type, transaction value, price
    transaction_computed = compute_transactions(extracted_data.get('price_transaction'))
    extracted_data.update({'price': transaction_computed.get('price')})
    extracted_data.update({'transaction_value': transaction_computed.get('transaction_value')})
    extracted_data.update({'transaction_type': transaction_computed.get('transaction_type')})
   
    # Calculate amount transaction
    amount_transaction = abs(extracted_data.get('holding_before') - extracted_data.get('holding_after'))
    extracted_data.update({'amount_transaction': amount_transaction})

    extracted_data.update({'source': filename})

    return extracted_data
from pathlib import Path 

from idx_pipeline.config.settings import SUPABASE_CLIENT 
from idx_pipeline.utils.helper import write_json 


def get_idx_companies(): 
    try:
        response = (
            SUPABASE_CLIENT
            .table('idx_company_report')
            .select('company_name, symbol', 'sector', 'sub_sector')
            .execute()
        )
        return response.data

    except Exception as error:
        print(f"Error fetching SGX companies: {error}")
        return None


def refresh_master_company_data():     
    base_dir = Path('data_v2/idx_companies')

    datas = get_idx_companies()

    idx_lookup = {}
    
    for data in datas: 
        symbol = data.get('symbol') 
        idx_lookup[symbol] = data

    path = base_dir / f'company_map.json'
    write_json(idx_lookup, str(path))

    print(f"Saved {len(idx_lookup)} companies to data_v2/idx_companies/company_map.json")


if __name__ == '__main__':
    refresh_master_company_data()
   
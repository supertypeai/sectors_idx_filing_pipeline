from dotenv import load_dotenv 
from supabase import create_client 

import os 


load_dotenv()


PROXY = os.getenv('PROXY')
SUPABASE_URL = os.getenv('SUPABASE_URL')
SUPABASE_KEY = os.getenv('SUPABASE_KEY')

AWS_ACCESS_KEY_ID = os.getenv('AWS_ACCESS_KEY_ID')
AWS_SECRET_ACCESS_KEY = os.getenv('AWS_SECRET_ACCESS_KEY')
AWS_REGION = os.getenv('AWS_REGION')
SES_FROM_EMAIL = os.getenv('SES_FROM_EMAIL')
ALERT_TO_EMAIL = os.getenv('ALERT_TO_EMAIL')

GROQ_API_KEY_DEV = os.getenv('GROQ_API_KEY_DEV')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
GEMINI_API_KEY_BACKUP = os.getenv('GEMINI_API_KEY_BACKUP')


SUPABASE_CLIENT = create_client(SUPABASE_URL, SUPABASE_KEY)


from supabase import create_client, Client
from functools import lru_cache
import os


@lru_cache(maxsize=1)
def get_supabase() -> Client:
    url = os.environ["SUPABASE_URL"]
    key = os.environ["SUPABASE_SERVICE_KEY"]   # Service role — full access
    return create_client(url, key)

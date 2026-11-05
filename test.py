import os
from dotenv import load_dotenv

load_dotenv()

SERVICE_KEY = os.getenv("PUBLIC_API_KEY")

print(SERVICE_KEY) 
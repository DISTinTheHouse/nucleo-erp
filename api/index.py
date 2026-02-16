import os

# Configure Django settings for Vercel
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "ERP.settings")

# Export a WSGI-compatible app named `app` for Vercel's Python runtime
from ERP.wsgi import application as app


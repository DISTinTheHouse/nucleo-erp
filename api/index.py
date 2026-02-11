import os
from ERP.wsgi import application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "ERP.settings")

app = application

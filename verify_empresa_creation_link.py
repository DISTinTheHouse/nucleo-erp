import os
import django
import sys

# Setup Django environment
sys.path.append(os.getcwd())
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'ERP.settings')
django.setup()

from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from rest_framework.authtoken.models import Token
from nucleo.models import Empresa

Usuario = get_user_model()

def run_test():
    username = "test_linker_user"
    email = "linker@test.com"
    password = "password123"

    print(f"1. Cleaning up old user '{username}'...")
    if Usuario.objects.filter(username=username).exists():
        Usuario.objects.get(username=username).delete()
    
    # Clean up company if exists
    if Empresa.objects.filter(codigo="LINK-TEST").exists():
        Empresa.objects.filter(codigo="LINK-TEST").delete()

    print(f"2. Creating user '{username}' without company...")
    user = Usuario.objects.create_user(username=username, email=email, password=password)
    print(f"   Initial State -> Empresa: {user.empresa}, Is Admin: {user.is_admin_empresa}")

    print("3. Generating Token...")
    token, _ = Token.objects.get_or_create(user=user)
    
    print("4. Creating Empresa via API (APIClient)...")
    client = APIClient(SERVER_NAME='192.168.0.15') # Set valid host
    client.credentials(HTTP_AUTHORIZATION='Bearer ' + token.key)

    payload = {
        "codigo": "LINK-TEST",
        "razon_social": "Link Test Company",
        "rfc": "XAXX010101000",
        "tipo_persona": "moral",
        "regimen_fiscal": 601,
        "cp": "12345"
    }

    response = client.post('/api/v1/nucleo/empresas/', payload, format='json')

    if response.status_code == 201:
        print("   Company created successfully (HTTP 201).")
        
        # 5. Verify Linkage
        user.refresh_from_db()
        print(f"   Final State -> Empresa: {user.empresa}, Is Admin: {user.is_admin_empresa}")
        
        if user.empresa and user.empresa.codigo == "LINK-TEST" and user.is_admin_empresa:
            print("\n✅ SUCCESS: User was automatically linked to the new company.")
        else:
            print("\n❌ FAILURE: User was NOT linked correctly.")
    else:
        print(f"\n❌ Failed to create company: {response.status_code}")
        print(response.data)

if __name__ == "__main__":
    run_test()

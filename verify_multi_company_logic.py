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
    username = "test_multi_user"
    email = "multi@test.com"
    password = "password123"

    print(f"1. Cleaning up old user '{username}'...")
    if Usuario.objects.filter(username=username).exists():
        Usuario.objects.get(username=username).delete()
    
    # Clean up companies
    Empresa.objects.filter(codigo__in=["EMP-1", "EMP-2"]).delete()

    print("2. Creating user and first company manually...")
    empresa1 = Empresa.objects.create(codigo="EMP-1", razon_social="Empresa 1", rfc="XAXX010101000")
    user = Usuario.objects.create_user(username=username, email=email, password=password)
    user.empresa = empresa1
    user.save()
    
    print(f"   Initial State -> Active Empresa: {user.empresa}")

    print("3. Generating Token...")
    token, _ = Token.objects.get_or_create(user=user)
    
    print("4. Creating SECOND Empresa via API...")
    client = APIClient(SERVER_NAME='192.168.0.15')
    client.credentials(HTTP_AUTHORIZATION='Bearer ' + token.key)

    payload = {
        "codigo": "EMP-2",
        "razon_social": "Empresa 2 (New)",
        "rfc": "XAXX010101000",
        "tipo_persona": "moral",
        "regimen_fiscal": 601,
        "cp": "12345"
    }

    response = client.post('/api/v1/nucleo/empresas/', payload, format='json')

    if response.status_code == 201:
        print("   Company 2 created successfully (HTTP 201).")
        
        # 5. Verify Linkage (M2M)
        user.refresh_from_db()
        empresas_access = user.empresas.all()
        print(f"   User M2M Empresas count: {empresas_access.count()}")
        
        emp2 = Empresa.objects.get(codigo="EMP-2")
        if emp2 in empresas_access:
             print("   ✅ SUCCESS: User has M2M access to new company.")
        else:
             print("   ❌ FAILURE: User DOES NOT have M2M access to new company.")

        # 6. Verify Visibility via API
        print("6. Verifying API visibility (GET /nucleo/mis-empresas/)...")
        resp_list = client.get('/api/v1/nucleo/mis-empresas/')
        data = resp_list.json()
        print(f"   Companies found in API: {len(data)}")
        codes = [e['codigo'] for e in data]
        print(f"   Codes: {codes}")
        
        if "EMP-1" in codes and "EMP-2" in codes:
            print("   ✅ SUCCESS: Both companies are visible in API.")
        else:
            print("   ❌ FAILURE: Missing companies in API list.")

    else:
        print(f"\n❌ Failed to create company: {response.status_code}")
        print(response.data)

if __name__ == "__main__":
    run_test()

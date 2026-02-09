
import os
import django
import sys
import json
import requests

# Setup Django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'ERP.settings')
django.setup()

from django.contrib.auth import get_user_model
from rest_framework.authtoken.models import Token
from nucleo.models import Empresa

User = get_user_model()
BASE_URL = "http://localhost:8003/api/v1"

def create_users():
    print("--- Creating Users ---")
    # Superuser
    super_email = "super_admin_test@erp.com"
    super_pass = "SuperPass123!"
    if not User.objects.filter(email=super_email).exists():
        User.objects.create_superuser(
            email=super_email,
            username="super_admin_test",
            password=super_pass,
            first_name="Super",
            last_name="Admin"
        )
        print(f"Created superuser: {super_email}")
    else:
        print(f"Superuser exists: {super_email}")

    # Normal User
    norm_email = "normal_user_test@erp.com"
    norm_pass = "NormalPass123!"
    if not User.objects.filter(email=norm_email).exists():
        User.objects.create_user(
            email=norm_email,
            username="normal_user_test",
            password=norm_pass,
            first_name="Normal",
            last_name="User",
            is_staff=False,
            is_superuser=False
        )
        print(f"Created normal user: {norm_email}")
    else:
        print(f"Normal user exists: {norm_email}")
        
    return (super_email, super_pass), (norm_email, norm_pass)

def get_token(email, password):
    url = f"{BASE_URL}/login/"
    data = {"email": email, "password": password}
    try:
        resp = requests.post(url, json=data)
        if resp.status_code == 200:
            return resp.json(), None
        return None, resp.text
    except Exception as e:
        return None, str(e)

def test_permissions():
    (super_email, super_pass), (norm_email, norm_pass) = create_users()
    
    # 1. Test Login & is_superuser field
    print("\n--- Test 1: Login Response Structure ---")
    
    # Superuser Login
    super_data, error = get_token(super_email, super_pass)
    if super_data:
        print("✅ Superuser login successful")
        if super_data.get('is_superuser') is True:
            print("✅ 'is_superuser' is True for superuser")
        else:
            print(f"❌ 'is_superuser' is {super_data.get('is_superuser')} for superuser")
    else:
        print(f"❌ Superuser login failed: {error}")
        return

    # Normal User Login
    norm_data, error = get_token(norm_email, norm_pass)
    if norm_data:
        print("✅ Normal user login successful")
        if norm_data.get('is_superuser') is False:
            print("✅ 'is_superuser' is False for normal user")
        else:
            print(f"❌ 'is_superuser' is {norm_data.get('is_superuser')} for normal user")
    else:
        print(f"❌ Normal user login failed: {error}")
        return

    # 2. Test Empresa Creation (Restricted to Superuser)
    print("\n--- Test 2: Empresa Creation Permission ---")
    
    import random
    rand_suffix = random.randint(1000, 9999)
    empresa_data = {
        "codigo": f"TEST_PERM_{rand_suffix}",
        "nombre": "Empresa Test Permissions",
        "razon_social": "Empresa Test Permissions SA de CV",
        "rfc": "XAXX010101000",
        "direccion": "Calle Falsa 123",
        "cp": "12345",
        "ciudad": "Ciudad Test",
        "estado": "Estado Test",
        "pais": "MEX",
        "moneda": "MXN"
    }
    
    # Try with Normal User
    headers_norm = {"Authorization": f"Bearer {norm_data['token']}"}
    resp = requests.post(f"{BASE_URL}/nucleo/empresas/", json=empresa_data, headers=headers_norm)
    if resp.status_code == 403:
        print("✅ Normal user blocked from creating Empresa (403 Forbidden)")
    else:
        print(f"❌ Normal user allowed to create Empresa or other error: {resp.status_code} - {resp.text}")

    # Try with Superuser
    headers_super = {"Authorization": f"Bearer {super_data['token']}"}
    resp = requests.post(f"{BASE_URL}/nucleo/empresas/", json=empresa_data, headers=headers_super)
    empresa_id = None
    if resp.status_code in [200, 201]:
        print("✅ Superuser allowed to create Empresa")
        empresa_id = resp.json().get('id_empresa') or resp.json().get('id')
        print(f"   Created Empresa ID: {empresa_id}")
    else:
        print(f"❌ Superuser failed to create Empresa: {resp.status_code} - {resp.text}")

    # 2.1 Test Normal User GET List (Should be allowed but filtered)
    print("\n--- Test 2.1: Normal User GET Empresa List ---")
    resp = requests.get(f"{BASE_URL}/nucleo/empresas/", headers=headers_norm)
    if resp.status_code == 200:
        print(f"✅ Normal user allowed to list Empresas (CRUD Endpoint). Count: {len(resp.json()) if isinstance(resp.json(), list) else 'Page'}")
    else:
        print(f"❌ Normal user failed to list Empresas: {resp.status_code} - {resp.text}")

    print("\n--- Test 2.1b: Normal User GET Mis Empresas (Simple List) ---")
    resp = requests.get(f"{BASE_URL}/nucleo/mis-empresas/", headers=headers_norm)
    if resp.status_code == 200:
        print(f"✅ Normal user allowed to list Mis Empresas. Count: {len(resp.json())}")
    else:
        print(f"❌ Normal user failed to list Mis Empresas: {resp.status_code} - {resp.text}")

    # 2.2 Test Normal User Update (Should be blocked)
    if empresa_id:
        print("\n--- Test 2.2: Normal User Update Empresa ---")
        update_data = {"nombre": "Hacked Name"}
        resp = requests.patch(f"{BASE_URL}/nucleo/empresas/{empresa_id}/", json=update_data, headers=headers_norm)
        if resp.status_code == 403:
            print("✅ Normal user blocked from updating Empresa (403 Forbidden)")
        else:
            print(f"❌ Normal user allowed to update Empresa or other error: {resp.status_code} - {resp.text}")

        # Cleanup (Delete by Superuser)
        print("\n--- Cleanup ---")
        resp = requests.delete(f"{BASE_URL}/nucleo/empresas/{empresa_id}/", headers=headers_super)
        if resp.status_code == 204:
            print("✅ Superuser deleted test Empresa")
        else:
            print(f"⚠️ Failed to delete test Empresa: {resp.status_code}")

    # 3. Test Sucursal Creation (Restricted to Superuser)
    print("\n--- Test 3: Sucursal Creation Permission ---")
    # Need an existing empresa for sucursal
    # We will use the demo one or create one temporarily
    # For simplicity, just checking if we get 403 is enough validation of the permission class
    
    sucursal_data = {
        "codigo": "SUC_TEST",
        "nombre": "Sucursal Test",
        "empresa": 1 # Assuming ID 1 exists, or it will fail validation but permission check comes first
    }
    
    resp = requests.post(f"{BASE_URL}/nucleo/sucursales/", json=sucursal_data, headers=headers_norm)
    if resp.status_code == 403:
        print("✅ Normal user blocked from creating Sucursal (403 Forbidden)")
    else:
        print(f"❌ Normal user allowed to create Sucursal or other error: {resp.status_code} - {resp.text}")

    # 4. Test Usuario Creation (Restricted to Superuser)
    print("\n--- Test 4: Usuario Creation Permission ---")
    user_data = {
        "email": "new_user@test.com",
        "username": "new_user",
        "password": "Password123!"
    }
    resp = requests.post(f"{BASE_URL}/usuarios/usuarios/", json=user_data, headers=headers_norm)
    if resp.status_code == 403:
        print("✅ Normal user blocked from creating Usuario (403 Forbidden)")
    else:
        print(f"❌ Normal user allowed to create Usuario or other error: {resp.status_code} - {resp.text}")


if __name__ == "__main__":
    try:
        test_permissions()
    except Exception as e:
        print(f"Error running test: {e}")

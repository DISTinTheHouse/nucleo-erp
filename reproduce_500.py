import requests
import sys

BASE_URL = "http://192.168.0.15:8003/api/v1"

def test_create_company():
    # 1. Login to get token (using existing user if possible, or new one)
    # We'll register a new user to be clean
    import random
    import string
    suffix = ''.join(random.choices(string.ascii_lowercase + string.digits, k=5))
    
    print(f"1. Registering new user for test: user_{suffix}")
    register_payload = {
        "empresa_razon_social": f"Empresa Init {suffix}",
        "empresa_codigo": f"init-{suffix}",
        "empresa_rfc": "XAXX010101000",
        "empresa_email": f"init-{suffix}@test.com",
        "sucursal_nombre": "Matriz Init",
        "sucursal_codigo": f"SUC-INIT-{suffix}",
        "usuario_username": f"user_{suffix}",
        "usuario_email": f"user-{suffix}@test.com",
        "usuario_password": "Password123!",
        "usuario_first_name": "Test",
        "usuario_last_name": "User"
    }
    
    try:
        resp = requests.post(f"{BASE_URL}/onboarding/register/", json=register_payload, timeout=5)
    except requests.exceptions.ConnectionError:
        print("❌ Could not connect to server at 192.168.0.15:8003. Is it running?")
        sys.exit(1)

    if resp.status_code != 201:
        print(f"❌ Registration failed: {resp.text}")
        sys.exit(1)
        
    # 2. Login
    login_payload = {
        "email": register_payload["usuario_email"],
        "password": register_payload["usuario_password"]
    }
    resp = requests.post(f"{BASE_URL}/login/", json=login_payload)
    token = resp.json().get('token')
    print(f"   Token obtained: {token[:10]}...")
    
    # 3. Create SECOND company (this triggers the new logic)
    print("2. Attempting to create SECOND company via API...")
    headers = {"Authorization": f"Bearer {token}"}
    empresa_payload = {
        "codigo": f"EMP-NEW-{suffix}",
        "razon_social": "Empresa Nueva 500 Check",
        "rfc": "XAXX010101000",
        "tipo_persona": "moral",
        "regimen_fiscal": 601,
        "cp": "12345"
    }
    
    resp = requests.post(f"{BASE_URL}/nucleo/empresas/", json=empresa_payload, headers=headers)
    
    print(f"   Status Code: {resp.status_code}")
    print(f"   Response: {resp.text}")
    
    if resp.status_code == 500:
        print("✅ REPRODUCED: Server returned 500 Internal Server Error.")
    elif resp.status_code == 201:
        print("❌ NOT REPRODUCED: Creation succeeded.")
        
        # Verify visibility
        print("3. Verifying visibility (GET /nucleo/mis-empresas/)...")
        resp_list = requests.get(f"{BASE_URL}/nucleo/mis-empresas/", headers=headers)
        if resp_list.status_code == 200:
            data = resp_list.json()
            print(f"   Companies found: {len(data)}")
            if len(data) >= 2:
                print("   ✅ SUCCESS: Both companies visible.")
            else:
                print("   ⚠️ WARNING: Expected 2 companies, found " + str(len(data)))
        else:
             print(f"   ❌ Failed to list companies: {resp_list.status_code}")

    else:
        print(f"⚠️ Unexpected status: {resp.status_code}")

if __name__ == "__main__":
    test_create_company()

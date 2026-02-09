import requests
import json
import random
import string

# Config
BASE_URL = "http://127.0.0.1:8003/api/v1"
SUFFIX = ''.join(random.choices(string.ascii_lowercase + string.digits, k=5))

def print_section(title):
    print(f"\n{'='*50}\n{title}\n{'='*50}")

def run_test():
    # 1. Register
    print_section("1. REGISTER TENANT")
    register_payload = {
        "empresa_razon_social": f"Empresa API {SUFFIX}",
        "empresa_codigo": f"api-{SUFFIX}",
        "empresa_rfc": "XAXX010101000",
        "empresa_email": f"api-{SUFFIX}@test.com",
        "sucursal_nombre": "Matriz API",
        "sucursal_codigo": f"SUC-API-{SUFFIX}",
        "usuario_username": f"user_api_{SUFFIX}",
        "usuario_email": f"user-api-{SUFFIX}@test.com",
        "usuario_password": "Password123!",
        "usuario_first_name": "API",
        "usuario_last_name": "Tester"
    }
    resp = requests.post(f"{BASE_URL}/onboarding/register/", json=register_payload)
    print(f"Register Status: {resp.status_code}")
    if resp.status_code != 201:
        print(f"Error: {resp.text}")
        return
    
    # 2. Login
    print_section("2. LOGIN")
    login_payload = {
        "email": register_payload["usuario_email"],
        "password": register_payload["usuario_password"]
    }
    resp = requests.post(f"{BASE_URL}/login/", json=login_payload)
    print(f"Login Status: {resp.status_code}")
    if resp.status_code != 200:
        print(f"Error: {resp.text}")
        return
    
    token = resp.json()['token']
    headers = {"Authorization": f"Bearer {token}"} # Use Bearer as per BearerTokenAuthentication
    print(f"Token obtained: {token[:10]}...")

    # 3. Test Endpoints (CRUD Create/List)
    
    # Fetch Empresa ID
    print_section("3. FETCH EMPRESAS")
    resp = requests.get(f"{BASE_URL}/nucleo/mis-empresas/", headers=headers)
    print(f"Mis Empresas Status: {resp.status_code}")
    if resp.status_code != 200:
        print(f"Error fetching companies: {resp.text}")
        return
        
    empresas = resp.json()
    print(f"Empresas found: {json.dumps(empresas, indent=2)}")
    
    if not isinstance(empresas, list) or len(empresas) == 0:
        print("No companies found for user or invalid format.")
        return
        
    empresa_id = empresas[0]['id']
    print(f"Using Company ID: {empresa_id}")

    # Fetch Sucursales (to get one for Department)
    print_section("3.1 FETCH SUCURSALES")
    resp = requests.get(f"{BASE_URL}/nucleo/mis-sucursales/", headers=headers)
    sucursales = resp.json()
    if not sucursales:
        print("No branches found.")
        return
    sucursal_id = sucursales[0]['id']
    print(f"Using Branch ID: {sucursal_id}")

    # Monedas
    print_section("4. TEST MONEDAS")
    # ISO codes are 3 chars
    moneda_payload = {"codigo_iso": f"T{SUFFIX[:2].upper()}", "nombre": "Test Coin", "simbolo": "$"}
    resp = requests.post(f"{BASE_URL}/nucleo/monedas/", json=moneda_payload, headers=headers)
    print(f"Create Moneda: {resp.status_code}")
    if resp.status_code >= 300:
        print(resp.text)
    else:
        print(resp.json())

    # Departamentos
    print_section("5. TEST DEPARTAMENTOS")
    dept_payload = {
        "nombre": "IT Dept", 
        "codigo": f"IT-{SUFFIX}",
        "empresa": empresa_id,
        "sucursal": sucursal_id
    }
    resp = requests.post(f"{BASE_URL}/nucleo/departamentos/", json=dept_payload, headers=headers)
    print(f"Create Departamento: {resp.status_code}")
    if resp.status_code >= 300:
        print(resp.text)
    else:
        print(resp.json())

    # Roles
    print_section("6. TEST ROLES")
    rol_payload = {
        "empresa": empresa_id,
        "codigo": f"ROL-{SUFFIX}",
        "nombre": "Rol Test API",
        "clave_departamento": f"IT-{SUFFIX}"
    }
    resp = requests.post(f"{BASE_URL}/seguridad/roles/", json=rol_payload, headers=headers)
    print(f"Create Rol: {resp.status_code}")
    if resp.status_code >= 300:
        print(resp.text)
    else:
        print(resp.json())

    # Sucursales
    print_section("7. TEST SUCURSALES")
    suc_payload = {
        "empresa": empresa_id,
        "nombre": "Sucursal Norte",
        "codigo": f"NORTE-{SUFFIX}",
        "direccion": "Calle Falsa 123",
        "telefono": "5551234567"
    }
    resp = requests.post(f"{BASE_URL}/nucleo/sucursales/", json=suc_payload, headers=headers)
    print(f"Create Sucursal: {resp.status_code}")
    if resp.status_code >= 300:
        print(resp.text)
    else:
        print(resp.json())

    # Usuarios
    print_section("8. TEST USUARIOS")
    user_payload = {
        "username": f"newuser_{SUFFIX}",
        "email": f"newuser-{SUFFIX}@test.com",
        "password": "Password123!",
        "empresa": empresa_id,
        "first_name": "New",
        "last_name": "User"
    }
    resp = requests.post(f"{BASE_URL}/usuarios/usuarios/", json=user_payload, headers=headers)
    print(f"Create Usuario: {resp.status_code}")
    if resp.status_code >= 300:
        print(resp.text)
    else:
        print(resp.json())

if __name__ == "__main__":
    run_test()

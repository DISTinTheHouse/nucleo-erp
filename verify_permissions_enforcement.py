import requests
import json
import os
import django
import sys

# Setup Django
sys.path.append(os.getcwd())
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'ERP.settings')
django.setup()

from django.contrib.auth import get_user_model
User = get_user_model()

BASE_URL = "http://localhost:8003/api/v1"

def create_superuser():
    username = "superuser_test"
    email = "superuser@test.com"
    password = "SuperPass123!"
    if not User.objects.filter(username=username).exists():
        User.objects.create_superuser(username, email, password)
        print(f"✅ Superusuario creado: {email}")
    else:
        u = User.objects.get(username=username)
        u.is_superuser = True
        u.is_staff = True
        u.set_password(password)
        u.save()
        print(f"ℹ️ Superusuario existente actualizado: {email}")
    return email, password

def create_normal_user():
    # Use onboarding to ensure correct setup
    url = f"{BASE_URL}/onboarding/register/"
    data = {
        "empresa_razon_social": "Empresa Normal S.A.",
        "empresa_codigo": "EMP-NORM",
        "empresa_rfc": "XAXX010101000",
        "empresa_email": "normal@empresa.com",
        "sucursal_nombre": "Sucursal Normal",
        "sucursal_codigo": "SUC-NORM",
        "usuario_username": "normal_user",
        "usuario_email": "normal@empresa.com",
        "usuario_password": "NormalPass123!",
        "usuario_first_name": "Normal",
        "usuario_last_name": "User"
    }
    # Check if exists
    if User.objects.filter(email="normal@empresa.com").exists():
        print("ℹ️ Usuario normal ya existe.")
        return "normal@empresa.com", "NormalPass123!"
        
    resp = requests.post(url, json=data)
    if resp.status_code == 201:
        print("✅ Usuario normal creado via Onboarding.")
    else:
        print(f"⚠️ Error creando usuario normal: {resp.text}")
    return "normal@empresa.com", "NormalPass123!"

def get_token(email, password):
    resp = requests.post(f"{BASE_URL}/login/", json={"email": email, "password": password})
    if resp.status_code == 200:
        data = resp.json()
        return data['token'], data.get('es_admin', False), data.get('is_superuser', False)
    print(f"❌ Login failed for {email}: {resp.text}")
    return None, False, False

def test_permissions():
    # 1. Setup Users
    super_email, super_pass = create_superuser()
    norm_email, norm_pass = create_normal_user()
    
    # 2. Login Normal User
    print("\n--- Test 1: Normal User Permissions ---")
    token_norm, es_admin_norm, is_super_norm = get_token(norm_email, norm_pass)
    print(f"Login Normal: Token obtained. Admin={es_admin_norm}, Super={is_super_norm}")
    
    if is_super_norm:
        print("❌ Error: Usuario normal detectado como superusuario!")
        
    headers_norm = {"Authorization": f"Bearer {token_norm}", "Content-Type": "application/json"}
    
    # Try Create Empresa (Should Fail)
    print("Intentando crear Empresa como usuario normal...")
    resp = requests.post(f"{BASE_URL}/nucleo/empresas/", headers=headers_norm, json={
        "codigo": "EMP-HACK", "razon_social": "Hack S.A.", "rfc": "XAXX010101000"
    })
    if resp.status_code == 403:
        print("✅ CORRECTO: Creación bloqueada (403 Forbidden).")
    else:
        print(f"❌ FALLO: Se permitió crear/otro error: {resp.status_code} {resp.text}")

    # Try List Empresas (Should Succeed but filtered)
    print("Intentando listar Empresas...")
    resp = requests.get(f"{BASE_URL}/nucleo/empresas/", headers=headers_norm)
    if resp.status_code == 200:
        print(f"✅ CORRECTO: Listado permitido. Items: {len(resp.json())}")
    else:
        print(f"❌ FALLO: Listado falló: {resp.status_code}")

    # 3. Login Superuser
    print("\n--- Test 2: Superuser Permissions ---")
    token_super, es_admin_super, is_super_super = get_token(super_email, super_pass)
    print(f"Login Super: Token obtained. Admin={es_admin_super}, Super={is_super_super}")
    
    if not is_super_super:
        print("❌ Error: Superusuario NO detectado como superusuario!")
        
    headers_super = {"Authorization": f"Bearer {token_super}", "Content-Type": "application/json"}
    
    # Try Create Empresa (Should Succeed)
    print("Intentando crear Empresa como Superuser...")
    emp_data = {
        "codigo": "EMP-SUPER", 
        "razon_social": "Super S.A.", 
        "rfc": "XAXX010101000",
        "regimen_fiscal": 1, # ID dummy
        "pais": "MEX",
        "moneda": "MXN"
    }
    resp = requests.post(f"{BASE_URL}/nucleo/empresas/", headers=headers_super, json=emp_data)
    if resp.status_code in [201, 400]: # 400 is fine (validation), 403 is bad
        print(f"✅ CORRECTO: Acceso permitido (Status: {resp.status_code}).")
        if resp.status_code == 400:
            print(f"   (Error de validación esperado: {resp.text[:100]}...)")
    else:
        print(f"❌ FALLO: Acceso denegado o error inesperado: {resp.status_code} {resp.text[:100]}...")

if __name__ == "__main__":
    test_permissions()

import requests
import json

BASE_URL = "http://192.168.0.15:8003/api/v1"

def test_restricted_flow():
    print("🚀 INICIANDO PRUEBAS DE RESTRICCIÓN DE EMPRESAS 🚀")
    
    # 1. LOGIN SUPERUSER
    print("\n1. Login como Superusuario (admin@test.com)...")
    resp = requests.post(f"{BASE_URL}/login/", json={
        "email": "admin@test.com",
        "password": "Admin123!"
    })
    
    if resp.status_code != 200:
        print(f"❌ Error Login Superuser: {resp.status_code} - {resp.text}")
        return
    
    token_admin = resp.json()['token']
    headers_admin = {"Authorization": f"Bearer {token_admin}"}
    print("✅ Login Admin Exitoso")

    # 2. CREAR EMPRESA COMO ADMIN (Debe funcionar)
    print("\n2. Crear Empresa como Superusuario...")
    empresa_payload = {
        "codigo": "EMP-ADMIN",
        "razon_social": "Empresa Admin Test",
        "rfc": "XAXX010101000",
        "estatus": "activo",
        "idioma": "es-MX",
        "timezone": "America/Mexico_City"
    }
    # Primero intentamos borrarla por si existe de pruebas anteriores
    # (Para esto necesitaríamos el ID, pero asumamos que el codigo es unique)
    # Mejor usamos un código random
    import random
    suffix = random.randint(1000, 9999)
    empresa_payload['codigo'] = f"EMP-ADM-{suffix}"
    
    resp_create = requests.post(f"{BASE_URL}/nucleo/empresas/", json=empresa_payload, headers=headers_admin)
    if resp_create.status_code == 201:
        print(f"✅ Empresa creada exitosamente: {resp_create.json()['id_empresa']}")
        id_empresa = resp_create.json()['id_empresa']
    else:
        print(f"❌ Falló creación como admin: {resp_create.status_code} - {resp_create.text}")
        return

    # 3. CREAR USUARIO NORMAL (Requiere empresa)
    print("\n3. Crear Usuario Normal (usuario_normal)...")
    # Necesitamos una sucursal primero, vamos a crearla (como admin)
    sucursal_payload = {
        "empresa": id_empresa,
        "codigo": f"SUC-{suffix}",
        "nombre": "Sucursal Test",
        "estatus": "activo"
    }
    resp_suc = requests.post(f"{BASE_URL}/nucleo/sucursales/", json=sucursal_payload, headers=headers_admin)
    if resp_suc.status_code != 201:
         print(f"❌ Falló creación sucursal: {resp_suc.text}")
         return
    id_sucursal = resp_suc.json()['id_sucursal']
    print("✅ Sucursal creada.")

    user_payload = {
        "username": f"user_norm_{suffix}",
        "email": f"user_{suffix}@normal.com",
        "password": "UserNormal123!",
        "first_name": "User",
        "last_name": "Normal",
        "empresa": id_empresa,
        "sucursal_default": id_sucursal,
        "estatus": "activo"
    }
    
    resp_user = requests.post(f"{BASE_URL}/usuarios/usuarios/", json=user_payload, headers=headers_admin)
    if resp_user.status_code == 201:
        print("✅ Usuario normal creado.")
    else:
        print(f"❌ Falló creación usuario: {resp_user.status_code} - {resp_user.text}")
        return

    # 4. LOGIN USUARIO NORMAL
    print("\n4. Login como Usuario Normal...")
    resp_login_norm = requests.post(f"{BASE_URL}/login/", json={
        "email": f"user_{suffix}@normal.com",
        "password": "UserNormal123!"
    })
    token_norm = resp_login_norm.json()['token']
    headers_norm = {"Authorization": f"Bearer {token_norm}"}
    print("✅ Login Normal Exitoso")

    # 5. INTENTO DE CREAR EMPRESA COMO NORMAL (Debe fallar)
    print("\n5. Intento de Crear Empresa como Usuario Normal (Debe fallar 403)...")
    resp_fail = requests.post(f"{BASE_URL}/nucleo/empresas/", json=empresa_payload, headers=headers_norm)
    
    if resp_fail.status_code == 403:
        print("✅ CORRECTO: Acceso denegado (403 Forbidden).")
    else:
        print(f"❌ ERROR: Se permitió la creación o código incorrecto: {resp_fail.status_code}")

    # 6. INTENTO DE LISTAR TODAS LAS EMPRESAS (Debe ver solo la suya)
    print("\n6. Listar empresas como Usuario Normal...")
    resp_list = requests.get(f"{BASE_URL}/nucleo/empresas/", headers=headers_norm)
    data = resp_list.json()
    count = data['count'] if 'count' in data else len(data)
    print(f"   Empresas visibles: {count}")
    # Debería ver al menos 1 (la suya)
    if count >= 1:
        print("✅ Ve su empresa.")
    else:
        print("⚠️ No ve ninguna empresa (verificar filtros).")

if __name__ == "__main__":
    test_restricted_flow()

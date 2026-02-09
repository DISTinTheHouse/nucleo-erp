import requests
import json
import random
import string

# Configuración
BASE_URL = "http://192.168.0.15:8003/api/v1"
HEADERS = {"Content-Type": "application/json"}

def get_random_string(length=5):
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=length))

def run_simulation():
    print("🚀 INICIANDO SIMULACIÓN DE FLUJO FRONTEND (NEXT.JS) 🚀")
    print("-----------------------------------------------------")

    # 1. REGISTRO DE USUARIO INICIAL (Simulando onboarding o creación manual)
    suffix = get_random_string()
    username = f"frontend_user_{suffix}"
    email = f"frontend_{suffix}@test.com"
    password = "Password123!"
    
    print(f"\n1. [POST] Registrando usuario nuevo: {username}")
    # Usamos el endpoint de onboarding para crear usuario + empresa inicial de un golpe
    payload_register = {
        "empresa_razon_social": f"Empresa Init {suffix}",
        "empresa_codigo": f"INIT-{suffix}",
        "empresa_rfc": "XAXX010101000",
        "empresa_email": email,
        "sucursal_nombre": "Matriz",
        "sucursal_codigo": f"SUC-{suffix}",
        "usuario_username": username,
        "usuario_email": email,
        "usuario_password": password,
        "usuario_first_name": "Frontend",
        "usuario_last_name": "Tester"
    }
    
    resp_reg = requests.post(f"{BASE_URL}/onboarding/register/", json=payload_register, headers=HEADERS)
    if resp_reg.status_code != 201:
        print(f"❌ Error en registro: {resp_reg.status_code} - {resp_reg.text}")
        return
    print("✅ Usuario registrado exitosamente.")

    # 2. LOGIN (Obtener Token)
    print(f"\n2. [POST] Iniciando sesión (Login) para obtener Token...")
    # Ajuste: El endpoint de login espera 'email' en lugar de 'username' según el backend
    resp_login = requests.post(f"{BASE_URL}/login/", json={"email": email, "password": password}, headers=HEADERS)
    if resp_login.status_code != 200:
        print(f"❌ Error en login: {resp_login.status_code} - {resp_login.text}")
        # Intento con username por si acaso la configuración cambió
        print("   Reintentando con username...")
        resp_login = requests.post(f"{BASE_URL}/login/", json={"username": username, "password": password}, headers=HEADERS)
        if resp_login.status_code != 200:
             return
    
    token = resp_login.json().get('token')
    print(f"✅ Token obtenido: {token[:10]}...")
    
    auth_headers = HEADERS.copy()
    # Ajuste: Usamos 'Bearer' porque el backend usa BearerTokenAuthentication
    auth_headers["Authorization"] = f"Bearer {token}"

    # 3. VERIFICAR EMPRESA INICIAL EN 'MIS EMPRESAS'
    print(f"\n3. [GET] Verificando listado inicial (/nucleo/mis-empresas/)...")
    resp_list_1 = requests.get(f"{BASE_URL}/nucleo/mis-empresas/", headers=auth_headers)
    empresas_1 = resp_list_1.json()
    print(f"   Empresas encontradas: {len(empresas_1)}")
    if len(empresas_1) != 1:
        print("❌ Error: Debería haber 1 empresa inicial.")
        return
    print("✅ Listado inicial correcto.")

    # 4. CREAR NUEVA EMPRESA (Simulando formulario 'Crear Empresa')
    new_company_code = f"NEW-{suffix}"
    print(f"\n4. [POST] Creando SEGUNDA empresa (/nucleo/empresas/)...")
    payload_company = {
        "codigo": new_company_code,
        "razon_social": f"Segunda Empresa {suffix}",
        "rfc": "XAXX010101000",
        "estatus": "activo",
        "timezone": "America/Mexico_City",
        "idioma": "es-MX"
    }
    
    # Asegurar headers de autorización
    print(f"   Usando Token: {token[:10]}...")
    resp_create = requests.post(f"{BASE_URL}/nucleo/empresas/", json=payload_company, headers=auth_headers)
    if resp_create.status_code != 201:
        print(f"❌ Error al crear empresa: {resp_create.status_code} - {resp_create.text}")
        return
    
    new_company_data = resp_create.json()
    print(f"✅ Empresa creada: ID {new_company_data['id_empresa']} - {new_company_data['razon_social']}")

    # 5. VALIDAR VINCULACIÓN AUTOMÁTICA
    print(f"\n5. [GET] Validando vinculación en '/nucleo/mis-empresas/'...")
    resp_list_2 = requests.get(f"{BASE_URL}/nucleo/mis-empresas/", headers=auth_headers)
    empresas_2 = resp_list_2.json()
    
    print(f"   Empresas encontradas: {len(empresas_2)}")
    company_codes = [e['codigo'] for e in empresas_2]
    
    if len(empresas_2) >= 2 and new_company_code in company_codes:
        print(f"✅ ÉXITO: La nueva empresa ({new_company_code}) aparece automáticamente en el listado del usuario.")
        print("   Esto confirma que la vinculación (Usuario <-> Empresa) funciona correctamente.")
    else:
        print(f"❌ FALLO: La nueva empresa no aparece en el listado. Códigos encontrados: {company_codes}")

    # 6. VALIDAR ACCESO CRUD
    print(f"\n6. [GET] Validando acceso CRUD (/nucleo/empresas/)...")
    # El trailing slash es importante en Django
    resp_crud = requests.get(f"{BASE_URL}/nucleo/empresas/", headers=auth_headers)
    
    if resp_crud.status_code != 200:
        # Imprimir solo las primeras líneas del error si es HTML
        error_text = resp_crud.text[:500] if "html" in resp_crud.text else resp_crud.text
        print(f"❌ Error al obtener CRUD: {resp_crud.status_code}")
        return

    crud_data = resp_crud.json()
    
    # Dependiendo de la paginación, puede venir como lista o dict con 'results'
    results = crud_data.get('results', crud_data) if isinstance(crud_data, dict) else crud_data
    
    crud_codes = [e['codigo'] for e in results]
    if new_company_code in crud_codes:
        print(f"✅ ÉXITO: La nueva empresa es editable desde el CRUD.")
    else:
        print(f"❌ FALLO: La nueva empresa no aparece en el CRUD.")

if __name__ == "__main__":
    try:
        run_simulation()
    except Exception as e:
        print(f"\n❌ Error de ejecución: {str(e)}")

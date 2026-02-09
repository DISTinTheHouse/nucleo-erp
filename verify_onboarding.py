import requests
import json
import random
import string

def random_string(length=5):
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=length))

base_url = "http://127.0.0.1:8003/api/v1/onboarding/register/"
suffix = random_string()

payload = {
    "empresa_razon_social": f"Empresa Test {suffix} S.A.",
    "empresa_codigo": f"emp-test-{suffix}",
    "empresa_rfc": "XAXX010101000",
    "empresa_email": f"contacto-{suffix}@test.com",
    
    "sucursal_nombre": "Matriz Principal",
    "sucursal_codigo": f"SUC-{suffix}",
    
    "usuario_username": f"admin_{suffix}",
    "usuario_email": f"admin-{suffix}@test.com",
    "usuario_password": "Password123!",
    "usuario_first_name": "Test",
    "usuario_last_name": "User"
}

print(f"Enviando solicitud a {base_url}...")
print(f"Payload: {json.dumps(payload, indent=2)}")

try:
    response = requests.post(base_url, json=payload)
    print(f"Status Code: {response.status_code}")
    try:
        data = response.json()
        print(f"Response: {json.dumps(data, indent=2)}")
    except:
        print(f"Response Text: {response.text}")
        
    if response.status_code == 201:
        print("✅ TEST EXITOSO: El endpoint funciona correctamente.")
    else:
        print("❌ TEST FALLIDO: Hubo un error en la solicitud.")
        
except Exception as e:
    print(f"❌ ERROR DE CONEXIÓN: {e}")

# 📡 Documentación de API para Frontend (Next.js)

## 🌐 Configuración Base

- **Base URL Desarrollo**: `http://localhost:8000` (o tu IP local `192.168.0.X:8000`)
- **Autenticación**: Header `Authorization: Token <tu_token>`
- **Content-Type**: `application/json` (excepto para subida de archivos)

---

## 🚀 0. Onboarding (Registro de Nuevo Cliente)

Endpoint público para registrar una nueva Empresa, su primera Sucursal y el Usuario Administrador inicial en un solo paso (Transacción Atómica).

- **Endpoint**: `POST /api/v1/onboarding/register/`
- **Permisos**: Público (`AllowAny`)
- **Body**:
  ```json
  {
    "empresa_razon_social": "Mi Nueva Empresa S.A.",
    "empresa_codigo": "mi-empresa-sa",
    "empresa_rfc": "XAXX010101000",
    "empresa_email": "contacto@miempresa.com",

    "sucursal_nombre": "Matriz Principal",
    "sucursal_codigo": "SUC-001",

    "usuario_username": "admin_miempresa",
    "usuario_email": "admin@miempresa.com",
    "usuario_password": "SecurePassword123!",
    "usuario_first_name": "Juan",
    "usuario_last_name": "Pérez"
  }
  ```
- **Respuesta (201 Created)**:
  ```json
  {
    "message": "Registro completado exitosamente",
    "empresa": {
      "id": 10,
      "codigo": "mi-empresa-sa",
      "razon_social": "Mi Nueva Empresa S.A."
    },
    "usuario": {
      "id": 45,
      "username": "admin_miempresa",
      "email": "admin@miempresa.com"
    }
  }
  ```

---

## 🔐 1. Autenticación y Sesión

### Login

Obtén el token de sesión para el usuario.

- **Endpoint**: `POST /api/v1/login/`
- **Body**:
  ```json
  {
    "email": "admin@empresa.com",
    "password": "password123"
  }
  ```
- **Respuesta (200 OK)**:
  ```json
  {
    "token": "d834958c281321...",
    "user_id": 1,
    "email": "admin@empresa.com",
    "username": "admin",
    "nombre_completo": "Administrador Sistema",
    "es_admin": true
  }
  ```

---

## 🏢 2. Contexto de Usuario (Empresas y Sucursales)

### Mis Empresas

Lista las empresas a las que el usuario tiene acceso.

- **Endpoint**: `GET /api/v1/nucleo/mis-empresas/`
- **Respuesta**:
  ```json
  [
    {
      "id": 1,
      "codigo": "EMP001",
      "razon_social": "Mi Empresa S.A. de C.V.",
      "rfc": "XAXX010101000",
      "logo": "http://..."
    }
  ]
  ```

### Mis Sucursales

Lista las sucursales permitidas para el usuario dentro de una empresa específica.

- **Endpoint**: `GET /api/v1/nucleo/mis-sucursales/?empresa_id=1`
- **Respuesta**:
  ```json
  [
    {
      "id": 5,
      "codigo": "SUC-MTY",
      "nombre": "Sucursal Monterrey"
    }
  ]
  ```

---

## 📜 3. Catálogos del SAT (Facturación)

Recupera todos los catálogos fiscales necesarios para llenar formularios de facturación o configuración de empresa.

- **Endpoint**: `GET /api/v1/nucleo/sat/catalogos/`
- **Respuesta**:
  ```json
  {
    "regimenes_fiscales": [
      { "id_sat_regimen_fiscal": 1, "codigo": "601", "descripcion": "General de Ley Personas Morales", ... }
    ],
    "usos_cfdi": [...],
    "metodos_pago": [...],
    "formas_pago": [...]
  }
  ```

---

## ⚙️ 4. Configuración Fiscal (CSD)

Sube y valida los archivos de Certificado de Sello Digital (CSD) para una empresa.

### Obtener Configuración Actual

- **Endpoint**: `GET /api/v1/nucleo/empresas/{id_empresa}/config-sat/`
- **Respuesta**:
  ```json
  {
    "id_empresa_sat_config": 2,
    "validado": true,
    "no_certificado": "30001000000400002434",
    "fecha_expiracion": "2027-05-20T12:00:00Z",
    "mensaje_error": null,
    "regimen_fiscal": 1
  }
  ```

### Subir/Actualizar CSD (Archivos)

Este endpoint valida criptográficamente que el `.cer` y `.key` correspondan y que la contraseña sea correcta. También valida que el RFC del certificado coincida con el de la empresa.

- **Endpoint**: `PATCH /api/v1/nucleo/empresas/{id_empresa}/config-sat/`
- **Header**: `Content-Type: multipart/form-data`
- **Body (FormData)**:
  - `archivo_cer`: (File) Archivo .cer
  - `archivo_key`: (File) Archivo .key
  - `password_llave`: (Text) Contraseña de la llave privada
  - `regimen_fiscal`: (Int, Opcional) ID del régimen fiscal

- **Respuestas**:
  - `200 OK`: Archivos validados y guardados. `validado: true`.
  - `400 Bad Request`: Error de validación (ej. "Contraseña incorrecta", "RFC no coincide"). El campo `mensaje_error` contendrá el detalle.

---

## ⚠️ Notas de Integración

1.  **Validación de RFC**: Al crear o editar una empresa, el campo `rfc` se valida automáticamente (formato y checksum). Si es inválido, recibirás un `400 Bad Request`.
2.  **Seguridad**: Si se detectan múltiples intentos fallidos de login (5 intentos), la IP será bloqueada temporalmente por 1 hora (`django-axes`).

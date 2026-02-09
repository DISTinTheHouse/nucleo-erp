# 📡 Documentación de API para Frontend (Next.js)

## 🌐 Configuración Base

- **Base URL Desarrollo**: `http://localhost:8003` (o tu IP local `192.168.0.X:8003`)
- **Autenticación**: Header `Authorization: Bearer <tu_token>` (Excepto Login)
- **Content-Type**: `application/json` (excepto para subida de archivos)

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
    "es_admin": true,
    "is_superuser": true
  }
  ```

---

## 🏢 2. Contexto de Usuario (Empresas y Sucursales)

### Mis Empresas (Listado Simple)

Lista las empresas a las que el usuario tiene acceso explícito. Usar para el **Selector de Empresa**.

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

## 🏭 3. Gestión de Empresas (CRUD Completo)

Endpoint principal para administración de empresas.

**Permisos**:

- **Superusuario**: Acceso total (Crear, Leer Todas, Actualizar, Eliminar).
- **Usuario Normal**: Solo lectura (Lista filtrada a sus empresas asignadas). No puede crear ni editar.

- **Listar**: `GET /api/v1/nucleo/empresas/`
- **Crear**: `POST /api/v1/nucleo/empresas/` (Solo Superusuario)
- **Detalle**: `GET /api/v1/nucleo/empresas/{id}/`
- **Actualizar**: `PUT/PATCH /api/v1/nucleo/empresas/{id}/` (Solo Superusuario)

### Crear Empresa (Ejemplo - Solo Superusuario)

Al crear una empresa, el superusuario se asigna automáticamente a ella.

- **Endpoint**: `POST /api/v1/nucleo/empresas/`
- **Body**:
  ```json
  {
    "codigo": "EMP-NUEVA",
    "nombre_fiscal": "Nueva Empresa S.A.",
    "nombre_comercial": "Mi Nueva Empresa",
    "rfc": "XAXX010101000",
    "regimen_fiscal": "601",
    "codigo_postal": "64000",
    "pais": "MEX",
    "moneda": "MXN"
  }
  ```
- **Respuesta (201 Created)**: Objeto de la empresa creada.

---

## 📜 4. Catálogos del SAT (Facturación)

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

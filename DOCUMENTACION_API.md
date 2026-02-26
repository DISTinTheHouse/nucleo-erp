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
    "is_superuser": true,
    "is_admin_empresa": true,
    "empresa_id": 1,
    "permisos": ["R-CONF", "E-CONF", "D-CONF", "R-USU", "..."]
  }
  ```

- **Notas importantes para Frontend**:
  - `permisos` es un arreglo de claves de permiso efectivas para el usuario.
  - Incluye automáticamente:
    1. Permisos asignados por Roles.
    2. Overrides de tipo GRANT (UsuarioPermiso).
    3. Excluye Overrides de tipo DENY.
  - Las claves siguen el patrón `X-MODULO`, por ejemplo para el módulo Configuración:
    - `R-CONF` → Lectura
    - `E-CONF` → Edición
    - `D-CONF` → Eliminación
  - Para usuarios `is_superuser=true` o `is_admin_empresa=true`, el backend concede acceso amplio por rol; el frontend puede tratarlos como “tienen todo”, aunque la lista `permisos` pueda estar vacía.

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

### Sucursales (Gestión Completa)

Permite ver detalles y editar sucursales.

**Permisos**:

- **Superusuario**: Acceso total.
- **Admin Empresa**: Puede ver y editar (`PUT`/`PATCH`) las sucursales de su propia empresa.
- **Usuario Normal**: Solo lectura (filtrado por permisos).

- **Listar**: `GET /api/v1/nucleo/sucursales/`
- **Detalle**: `GET /api/v1/nucleo/sucursales/{codigo}/`
- **Editar**: `PATCH /api/v1/nucleo/sucursales/{codigo}/` (Requiere `is_admin_empresa=True`)

---

## 🛡️ 4. Roles y Permisos

### Gestión de Roles

Permite a un Admin de Empresa o Superusuario gestionar los roles y sus permisos asociados.

- **Base URL**: `/api/v1/seguridad/roles/`

### Asignar Permisos a un Rol

Endpoint específico para actualizar masivamente los permisos de un rol (Matrix de Permisos).

- **Endpoint**: `GET /api/v1/seguridad/roles/{id}/permisos/`
- **Descripción**: Obtiene la lista de IDs de permisos actualmente asignados al rol.
- **Respuesta (200 OK)**:
  ```json
  {
    "permisos": [1, 5, 8, 12]
  }
  ```

- **Endpoint**: `PUT /api/v1/seguridad/roles/{id}/permisos/`
- **Descripción**: Reemplaza completamente los permisos del rol con la nueva lista de IDs proporcionada.
- **Body**:
  ```json
  {
    "permisos": [1, 5, 8, 12, 15]
  }
  ```
- **Respuesta (200 OK)**:
  ```json
  {
    "status": "Permisos actualizados correctamente",
    "permisos": [1, 5, 8, 12, 15]
  }
  ```

---

## 👥 5. Gestión de Usuarios

API completa para gestionar el personal de la empresa (cajeros, vendedores, gerentes).

**Permisos**:

- **Superusuario**: Acceso total.
- **Admin Empresa**: Puede crear, editar y eliminar usuarios que pertenezcan a **su misma empresa**. No puede crear Superusuarios ni otros Admins de Empresa.

### Endpoints

- **Listar**: `GET /api/v1/usuarios/`
- **Crear**: `POST /api/v1/usuarios/`
- **Detalle**: `GET /api/v1/usuarios/{id}/`
- **Editar**: `PATCH /api/v1/usuarios/{id}/`
- **Eliminar**: `DELETE /api/v1/usuarios/{id}/`

### Ejemplo: Crear Usuario (Cajero)

El backend asigna automáticamente la empresa del administrador que crea el usuario.

- **Endpoint**: `POST /api/v1/usuarios/`
- **Body**:
  ```json
  {
    "username": "cajero_sucursal1",
    "email": "cajero@miempresa.com",
    "password": "Password123!",
    "first_name": "Juan",
    "last_name": "Perez",
    "sucursal_default": 5,
    "sucursales": [5],
    "estatus": "activo"
  }
  ```

---

## 🏭 4. Gestión de Empresas (CRUD Completo)

Endpoint principal para administración de empresas.

**Permisos**:

- **Superusuario**: Acceso total (Crear, Leer Todas, Actualizar, Eliminar).
- **Usuario Normal**: Solo lectura (Lista filtrada a sus empresas asignadas). No puede crear ni editar.

- **Listar**: `GET /api/v1/nucleo/empresas/`
- **Crear**: `POST /api/v1/nucleo/empresas/` (Solo Superusuario)
- **Detalle**: `GET /api/v1/nucleo/empresas/{id_o_codigo}/` (Acepta ID numérico o Código)
- **Actualizar**: `PUT/PATCH /api/v1/nucleo/empresas/{id_o_codigo}/` (Solo Superusuario)

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

## 📦 5. Inventarios (Almacenes y Ubicaciones)

Gestión de almacenes y ubicaciones para operaciones de inventario. La interacción de usuario se hace desde Next.js; el backend expone APIs con reglas de permisos y alcance.

Permisos:
- Lectura: cualquier usuario autenticado, datos filtrados por empresa y sucursales permitidas.
- Crear/Editar: requiere is_admin_empresa=true o superusuario.
- Eliminar: requiere is_admin_empresa=true o superusuario.

Alcance y reglas:
- Los listados se filtran por empresa activa y sucursales permitidas del usuario.
- Almacén fuerza consistencia: empresa = sucursal.empresa.
- Ubicación fuerza consistencia: empresa/sucursal se derivan del almacén.

Almacenes
- Listar: GET /api/v1/inventarios/almacenes/
- Detalle: GET /api/v1/inventarios/almacenes/{id_almacen}/
- Crear: POST /api/v1/inventarios/almacenes/
- Editar: PATCH /api/v1/inventarios/almacenes/{id_almacen}/
- Eliminar: DELETE /api/v1/inventarios/almacenes/{id_almacen}/

Ejemplo crear almacén
```
POST /api/v1/inventarios/almacenes/
Authorization: Bearer <token>
Content-Type: application/json

{
  "sucursal": 12,
  "codigo": "ALM-01",
  "nombre": "Almacén Central",
  "estatus": "activo"
}
```

Ubicaciones
- Listar: GET /api/v1/inventarios/ubicaciones/
- Detalle: GET /api/v1/inventarios/ubicaciones/{id_ubicacion}/
- Crear: POST /api/v1/inventarios/ubicaciones/
- Editar: PATCH /api/v1/inventarios/ubicaciones/{id_ubicacion}/
- Eliminar: DELETE /api/v1/inventarios/ubicaciones/{id_ubicacion}/

Ejemplo crear ubicación
```
POST /api/v1/inventarios/ubicaciones/
Authorization: Bearer <token>
Content-Type: application/json

{
  "almacen": 34,
  "codigo": "RACK-A1",
  "nombre": "Rack A1",
  "estatus": "activo"
}
```

Notas de respuesta y errores
- 200/201: operación exitosa.
- 400: validación o alcance inválido (empresa/sucursal fuera de permiso).
- 403: falta de privilegios para escribir/eliminar.

---

## ⚙️ 6. Configuración Fiscal (CSD)

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

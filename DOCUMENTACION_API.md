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

## 🔢 3. Series y Folios

Configuración de series y folios consecutivos para documentos (Facturas, Pedidos, etc.) por sucursal.

### Listar Series

Obtiene las series configuradas para la empresa del usuario.

- **Endpoint**: `GET /api/v1/nucleo/series-folios/`
- **Respuesta**:
  ```json
  [
    {
      "id_serie_folio": 1,
      "sucursal": 5,
      "tipo_documento": "FACTURA",
      "serie": "F",
      "folio_actual": 105,
      "relleno_ceros": 6,
      "separador": "-",
      "incluir_anio": true
    }
  ]
  ```

### Crear Serie

- **Endpoint**: `POST /api/v1/nucleo/series-folios/`
- **Body**:
  ```json
  {
    "sucursal": 5,
    "tipo_documento": "FACTURA",
    "serie": "F",
    "relleno_ceros": 6,
    "separador": "-",
    "incluir_anio": true
  }
  ```

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

## 🏭 6. Gestión de Empresas (CRUD Completo)

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

## 📜 7. Catálogos del SAT (Facturación)

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

### Claves de Producto/Servicio SAT

Catálogo extenso (50,000+ registros) para clasificar productos.
**Nota**: Soporta búsqueda por código o descripción.

- **Endpoint**: `GET /api/v1/nucleo/sat/prod-serv/?q={busqueda}`
- **Ejemplo**: `/api/v1/nucleo/sat/prod-serv/?q=computadora`
- **Respuesta**:
  ```json
  [
    {
      "id_sat_prodserv": 105,
      "codigo": "43211507",
      "descripcion": "Computadores de escritorio",
      "estatus": "activo"
    }
  ]
  ```

### Claves de Unidad SAT

Catálogo de unidades de medida (H87, KGM, etc.).
**Nota**: Soporta búsqueda.

- **Endpoint**: `GET /api/v1/nucleo/sat/unidades/?q={busqueda}`
- **Ejemplo**: `/api/v1/nucleo/sat/unidades/?q=pieza`
- **Respuesta**:
  ```json
  [
    {
      "id_sat_unidad": 5,
      "codigo": "H87",
      "descripcion": "Pieza",
      "estatus": "activo"
    }
  ]
  ```

### Unidades de Medida (CORE)

Unidades de medida internas del sistema (PZA, KG, MTR), utilizadas en los productos.
Generalmente se mapean a una clave SAT, pero este catálogo es el que se usa en la definición del producto.

- **Listar**: `GET /api/v1/nucleo/unidades-medida/`
- **Respuesta**:
  ```json
  [
    {
      "id": 1,
      "clave": "PZA",
      "nombre": "Pieza",
      "estatus": true
    }
  ]
  ```

### Impuestos

Catálogo de impuestos configurados en el sistema (IVA 16%, ISR, etc.).

- **Listar**: `GET /api/v1/nucleo/impuestos/`
- **Respuesta**:
  ```json
  [
    {
      "id": 1,
      "codigo": "IVA16",
      "nombre": "IVA 16%",
      "tasa": "0.160000",
      "tipo": "trasladado",
      "estatus": true
    }
  ]
  ```

---

## 📦 8. Inventarios (Almacenes y Ubicaciones)

Gestión de almacenes y ubicaciones para operaciones de inventario.

**Permisos**:

- **Lectura**: cualquier usuario autenticado, datos filtrados por empresa y sucursales permitidas.
- **Crear/Editar**: requiere `is_admin_empresa=true` o superusuario.
- **Eliminar**: requiere `is_admin_empresa=true` o superusuario.

**Alcance y reglas**:

- Los listados se filtran por empresa activa y sucursales permitidas del usuario.
- **Almacén**: fuerza consistencia `empresa = sucursal.empresa`.
- **Ubicación**: fuerza consistencia, empresa/sucursal se derivan del almacén.

### Almacenes

- **Listar**: `GET /api/v1/inventarios/almacenes/`
- **Respuesta**:
  ```json
  [
    {
      "id_almacen": 1,
      "empresa": 1,
      "sucursal": 5,
      "codigo": "ALM-MTY-01",
      "nombre": "Almacén Principal Monterrey",
      "estatus": "ACTIVO",
      "created_at": "2024-01-15T10:00:00Z",
      "updated_at": "2024-01-15T10:00:00Z"
    }
  ]
  ```
- **Detalle**: `GET /api/v1/inventarios/almacenes/{id_almacen}/`
- **Crear**: `POST /api/v1/inventarios/almacenes/`
- **Editar**: `PATCH /api/v1/inventarios/almacenes/{id_almacen}/`
- **Eliminar**: `DELETE /api/v1/inventarios/almacenes/{id_almacen}/`

### Ubicaciones

- **Listar**: `GET /api/v1/inventarios/ubicaciones/`
- **Respuesta**:
  ```json
  [
    {
      "id_ubicacion": 10,
      "empresa": 1,
      "sucursal": 5,
      "almacen": 1,
      "codigo": "P1-R3-N2",
      "nombre": "Pasillo 1, Rack 3, Nivel 2",
      "estatus": "ACTIVO",
      "created_at": "2024-01-15T11:00:00Z",
      "updated_at": "2024-01-15T11:00:00Z"
    }
  ]
  ```
- **Detalle**: `GET /api/v1/inventarios/ubicaciones/{id_ubicacion}/`
- **Crear**: `POST /api/v1/inventarios/ubicaciones/`
- **Editar**: `PATCH /api/v1/inventarios/ubicaciones/{id_ubicacion}/`
- **Eliminar**: `DELETE /api/v1/inventarios/ubicaciones/{id_ubicacion}/`

### Existencias (Stock)

Permite consultar el inventario actual.
**Nota de Seguridad**: Los resultados se filtran automáticamente según las sucursales y empresas permitidas para el usuario.

- **Listar**: `GET /api/v1/inventarios/existencias/`
- **Respuesta**:
  ```json
  [
    {
      "id": 105,
      "producto": {
        "id": 1,
        "nombre": "Camiseta Básica",
        "sku": "CAM-BAS-NEG-M"
      },
      "almacen": {
        "id": 1,
        "nombre": "Almacén Principal Monterrey"
      },
      "ubicacion": {
        "id": 10,
        "nombre": "Pasillo 1, Rack 3, Nivel 2"
      },
      "lote": null,
      "serie": null,
      "cantidad": 50.00
    }
  ]
  ```
- **Crear/Editar**: `POST/PATCH` (Restringido a Admin Empresa/Superusuario. Valida que el almacén pertenezca al scope del usuario).

### Movimientos de Inventario

Historial de entradas y salidas de mercancía.
**Nota de Seguridad**: Filtrado por scope de usuario.

- **Listar**: `GET /api/v1/inventarios/movimientos/`
- **Respuesta**:
  ```json
  [
    {
      "id": 204,
      "empresa": 1,
      "sucursal": 5,
      "tipo_movimiento": "ENTRADA",
      "fecha": "2024-02-01T14:30:00Z",
      "pedido": 1005,
      "entrega": null,
      "devolucion": null,
      "ajuste_inventario": null,
      "detalles": [
        { "producto": "Camiseta Básica", "cantidad": 10 }
      ]
    }
  ]
  ```
- **Crear**: `POST /api/v1/inventarios/movimientos/` (Requiere permisos de escritura y valida scope de empresa/sucursal).

### Detalles de Movimiento de Inventario

Gestiona los productos individuales dentro de un movimiento de inventario.
**Nota de Seguridad**: Valida estrictamente que la empresa y sucursal del movimiento coincidan con los permisos del usuario.

- **Listar**: `GET /api/v1/inventarios/movimiento-detalle/`
- **Crear**: `POST /api/v1/inventarios/movimiento-detalle/`
  - **Body**:
    ```json
    {
      "movimiento_inventario": 204,
      "producto": 1,
      "cantidad": 5,
      "costo_unitario": "150.00",
      "ubicacion_origen": 10,
      "ubicacion_destino": 11
    }
    ```
- **Editar**: `PATCH /api/v1/inventarios/movimiento-detalle/{id}/`
- **Eliminar**: `DELETE /api/v1/inventarios/movimiento-detalle/{id}/`

### Ajustes de Inventario

Permite registrar ajustes manuales (positivos o negativos) al inventario por pérdidas, daños o conteos cíclicos.
**Nota de Seguridad**: Requiere permisos de escritura y valida scope de empresa/sucursal.

- **Listar**: `GET /api/v1/inventarios/ajustes/`
- **Crear**: `POST /api/v1/inventarios/ajustes/`
  - **Body**:
    ```json
    {
      "empresa": 1,
      "sucursal": 5,
      "almacen": 1,
      "fecha_ajuste": "2024-02-10",
      "motivo": "Daño en almacén",
      "observaciones": "Caja mojada durante limpieza"
    }
    ```
- **Editar**: `PATCH /api/v1/inventarios/ajustes/{id}/`
- **Eliminar**: `DELETE /api/v1/inventarios/ajustes/{id}/`

---

## 🏷️ 9. Catálogo de Productos

Gestión de productos, variantes, y catálogos auxiliares (Tallas, Colores, Categorías).

**Base URL**: `/api/v1/catalogo/`

### Productos

Entidad principal que agrupa las variantes. Contiene la información general (nombre, descripción, categoría, impuestos).

- **Listar**: `GET /api/v1/catalogo/producto/`
- **Respuesta**:
  ```json
  [
    {
      "id": 1,
      "empresa": 1,
      "categoria_producto": 2,
      "unidad_medida": 1,
      "impuesto": 1,
      "sat_prodserv": 5,
      "sat_unidad": 3,
      "nombre": "Camiseta Básica",
      "descripcion": "Camiseta de algodón 100%",
      "tipo": "Producto Terminado",
      "activo": true,
      "created_at": "2024-02-01T09:00:00Z",
      "updated_at": "2024-02-01T09:00:00Z"
    }
  ]
  ```
- **Crear**: `POST /api/v1/catalogo/producto/`
- **Editar**: `PATCH /api/v1/catalogo/producto/{id}/`
- **Eliminar**: `DELETE /api/v1/catalogo/producto/{id}/`

### Variantes de Producto

Gestiona las combinaciones específicas (SKU, color, talla, precio).

- **Listar**: `GET /api/v1/catalogo/producto-variante/`
- **Respuesta**:
  ```json
  [
    {
      "id": 101,
      "producto": 1,
      "empresa": 1,
      "color": 3,
      "talla": 2,
      "sku": "CAM-BAS-NEG-M",
      "precio_base": "150.00",
      "activo": true
    }
  ]
  ```
- **Crear**: `POST /api/v1/catalogo/producto-variante/`
- **Editar**: `PATCH /api/v1/catalogo/producto-variante/{id}/`
- **Eliminar**: `DELETE /api/v1/catalogo/producto-variante/{id}/`

### Catálogos Auxiliares

Todos soportan CRUD estándar (`GET`, `POST`, `PATCH`, `DELETE`).

- **Tipos de Producto**: `/api/v1/catalogo/tipo-producto/`
- **Categorías**: `/api/v1/catalogo/categoria-producto/`
- **Colores**: `/api/v1/catalogo/color/`
- **Tallas**: `/api/v1/catalogo/talla/`

---

## ⚙️ 10. Configuración Fiscal (CSD)

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

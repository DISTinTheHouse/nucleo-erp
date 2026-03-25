# 📡 Documentación de API para Frontend (Next.js)

## 🌐 Configuración Base

- **Base URL Desarrollo**: `http://localhost:8003` (o tu IP local `192.168.0.X:8003`)
- **Autenticación**: Header `Authorization: Bearer <tu_token>` (Excepto Login)
- **Content-Type**: `application/json` (excepto para subida de archivos)

## 🏢 Aislamiento por Empresa (Multi-tenant) — Notas Importantes

La mayoría de endpoints operativos están **acotados por la empresa del usuario** (backend aplica scoping por `empresa` en el servidor).

- **Listados (GET collection)**: si el usuario no tiene `empresa_id` o no hay registros para su empresa, la respuesta esperada es `200 OK` con arreglo vacío `[]`.
- **Detalle (GET /{id}/)**: si el registro no pertenece a la empresa del usuario, el endpoint normalmente responderá `404 Not Found` (no se expone existencia cross-empresa).
- **Superusuario**: puede ver información global según el módulo (sin scoping).
- **Creación/edición**: cuando un recurso requiere `empresa`, usar siempre el `empresa_id` recibido en Login (no inventarlo ni cambiarlo desde el cliente).

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

### Catálogo de Permisos (para Matrix)

Endpoint para listar el catálogo global de permisos. Este endpoint se usa para pintar la tabla/matriz de permisos en frontend.

- **Endpoint**: `GET /api/v1/seguridad/permisos/`
- **Permisos**: Usuario autenticado (cualquier rol).
- **Query Params (opcionales)**:
  - `q`: búsqueda por `clave`, `nombre` o `descripcion`
  - `modulo`: filtra por módulo (ej. `ventas`, `clientes`)
- **Ejemplo**: `GET /api/v1/seguridad/permisos/?modulo=clientes&q=read`
- **Respuesta (200 OK)**:
  ```json
  [
    {
      "id": 10,
      "clave": "R-CLIE",
      "nombre": "read",
      "descripcion": "Permite ver clientes",
      "modulo": "clientes"
    }
  ]
  ```

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
    "roles": [3],
    "estatus": "activo"
  }
  ```

### Roles (asignación por API)

- **Listar roles disponibles (para selector)**: `GET /api/v1/seguridad/roles/`
  - Devuelve roles filtrados automáticamente por la empresa del usuario (si no es superusuario).
- **Asignar roles al crear/editar usuario**:
  - Enviar `roles` como lista de IDs (reemplaza la asignación actual).
  - Ejemplo: `PATCH /api/v1/usuarios/{id}/` con body `{ "roles": [3, 5] }`
  - La respuesta incluye `roles_ids` con los IDs asignados.

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
      "cantidad": 50.0
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
      "detalles": [{ "producto": "Camiseta Básica", "cantidad": 10 }]
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

## 👤 Terceros

### Direcciones Cliente

Listado de direcciones registradas de los clientes, incluyendo información de ubicación y configuración.

### Obtener Listado

- **Endpoint**: `GET terceros/direcciones-clientes/`
- **Respuesta**:
  ```json
  [
    {
      "id": 1,
      "is_default": true,
      "activo": true,
      "cliente": 1,
      "empresa": 73
    }
  ]
  ```

### Obtener registro individual por ID.

- **Endpoint**: `GET terceros/direcciones-clientes/{id}/`
- **Respuesta**:
  ```json
  {
    "id": 1,
    "is_default": true,
    "activo": true,
    "cliente": 1,
    "empresa": 73
  }
  ```

### Guardar direccion cliente.

- **Endpoint**: `POST terceros/direcciones-clientes/`
- **Body**:
  ```json
  {
    "cliente": 1,
    "empresa": 62,
    "is_default": true
  }
  ```

### Actualizar registro.

- **Endpoint**: `PATCH terceros/direcciones-clientes/{id}/`
- **Body**:
  ```json
  {
    "cliente": 1,
    "empresa": 62,
    "is_default": true
  }
  ```

---

## 🧾 Ventas - Cotizaciones (Onboarding)

**Base URL**: `/api/v1/ventas/`

El vendedor realiza el onboarding desde **Cotizaciones**. Al guardar la cotización:

- se crea/actualiza un registro en **Pedidos** ligado a la cotización (mesa de control) con `estatus=Por Autorizar`
- se genera el folio de pedido (ej: `P-000001`)
- el detalle (productos/tallas/bordado) se guarda en la estructura de pedido:
  - `PedidoDetalle`: 1 registro por **producto**
  - `PedidoDetalleTalla`: sub-líneas por **talla** (`cantidad` + `lleva_bordado` + `bordado_config`)

### 1) Obtener datos para el formulario (búsquedas y catálogos)

- **Endpoint**: `GET /api/v1/ventas/cotizaciones/onboarding/`
- **Query Params (opcionales)**:
  - `cliente_q`: texto para buscar cliente (nombre / razón social / RFC)
  - `producto_q`: texto para buscar producto (nombre)
  - `limit`: máximo 1–50 (default 20)
- **Respuesta (resumen)**:
  ```json
  {
    "vendedor": {
      "id": 1,
      "username": "user",
      "email": "user@mail.com",
      "empresa_id": 1
    },
    "catalogos": {
      "formas_pago": [{ "value": "01", "label": "01 - Efectivo" }],
      "metodos_pago": [
        { "value": "PUE", "label": "PUE - Pago en una sola exhibición" }
      ],
      "usos_cfdi": [{ "value": "G03", "label": "G03 - Gastos en general" }],
      "tallas": [{ "id": 1, "nombre": "CH" }],
      "tipos_pedido": [{ "value": 1, "label": "Stock" }, { "value": 2, "label": "Fabricacion" }],
      "regimenes_fiscales": [
        { "value": "601", "label": "601 - General de Ley Personas Morales" }
      ]
      "clientes": [{
        "id": 10,
        "razon_social": "Cliente SA",
        "nombre": "Cliente",
        "rfc": "XAXX010101000",
        "correo": "cliente@demo.com",
        "telefono": "8110000000",
        "direccion_fiscal": "Calle 1",
        "colonia": "Centro",
        "codigo_postal": "64000",
        "ciudad": "Monterrey",
        "estado": "NL",
        "giro_empresarial": "Textil",
        "sat_regimen_fiscal_id": 3,
        "sat_regimen_fiscal__codigo": "601",
        "sat_regimen_fiscal__descripcion": "General de Ley Personas Morales"
      }],
        }
      ],
      "productos": [
        {
          "id": 50,
          "nombre": "BATA EJECUTIVA DAMA BLANCO",
          "precio_base": "0.00"
        }
      ]
  ```

### 2) Crear cotización y generar pedido para mesa de control (con detalle)

- **Endpoint**: `POST /api/v1/ventas/cotizaciones/onboarding/`

**Reglas del flujo**

- El backend crea **1 `PedidoDetalle` por producto** (aunque se repita el producto en el payload).
- Las tallas repetidas se consolidan sumando `cantidad`.
- Si una talla viene con `lleva_bordado=true`, entonces `bordado_config` es requerido y se guarda en `PedidoDetalleTalla.bordado_config`.
- Se crea el `Pedido` ligado a la cotización con `estatus=Por Autorizar` para que mesa de control valide.

**Generación de folio**

- El folio se asigna automáticamente al crear el pedido (ej: `P-000001`) usando `SerieFolio` por sucursal y `tipo_documento="Pedido"`.

**Body (ejemplo)**

```json
{
  "cotizacion": {
    "sucursal": 1,
    "cliente": 10,
    "moneda": 1,
    "persona_pagos": "Juan Pérez",
    "correo_facturas": "facturas@cliente.com",
    "telefono_pagos": "8110000000",
    "forma_pago": "01",
    "metodo_pago": "PUE",
    "uso_cfdi": "G03",
    "embarque_parcial": false,
    "envio": "0.00",
    "flete": "0.00",
    "seguros": "0.00",
    "observaciones": "Notas opcionales"
  },
  "detalle": [
    {
      "producto": 50,
      "tallas": [
        {
          "talla": 1,
          "cantidad": 6,
          "lleva_bordado": true,
          "bordado_config": {
            "ubicaciones": [
              { "codigo": "F", "ancho_cm": 0, "alto_cm": 0, "color_hilo": null }
            ],
            "notas": "Opcional"
          }
        },
        { "talla": 2, "cantidad": 4, "lleva_bordado": false }
      ]
    }
  ]
}
```

**Respuesta**

```json
{
  "cotizacion": { "id": 10 },
  "pedido": {
    "id": 123,
    "folio": "P-000001",
    "folio_consecutivo": 1,
    "estatus": 2
  },
  "detalles": [
    {
      "id": 555,
      "pedido": 123,
      "pedido_folio": "P-000001",
      "producto": 50,
      "tallas": [
        {
          "id": 901,
          "talla": 1,
          "cantidad": 6,
          "lleva_bordado": true,
          "bordado_config": { "ubicaciones": [] }
        }
      ]
    }
  ]
}
```

### 3) Edición con ventana de tiempo + notificación a mesa de control

- **Endpoint**: `PATCH /api/v1/ventas/cotizaciones/{id}/`
- Regla: la edición está permitida dentro del periodo configurado; al editar, el pedido ligado se marca nuevamente como `Por Autorizar` (`estatus=2`) para revisión.
- Para editar también el detalle (productos/tallas/bordado) usando el mismo flujo, re-envía `POST /api/v1/ventas/cotizaciones/onboarding/` agregando `cotizacion_id` (y el detalle completo actualizado).

---

## 🧮 Mesa de Control

- Ver cotizaciones pendientes: filtra por `estatus=2 (Por Autorizar)` y `estatus=5 (Cambios Por Autorizar)`.
- Autorizar cotización:
  - **Endpoint**: `POST /api/v1/ventas/cotizaciones/{id}/autorizar/`
  - Efecto: se **duplica** la cotización a `Pedido` con folio `P-xxxxxx`, se marca la cotización como `Autorizada (3)` y se guarda un `aprobado_snapshot` del estado aprobado.
- Rechazar cotización:
  - **Endpoint**: `POST /api/v1/ventas/cotizaciones/{id}/rechazar/`
  - Efecto: la cotización pasa a `Rechazada (4)`. **No** se crea pedido ni se gasta folio.
- Aceptar cambios:
  - **Endpoint**: `POST /api/v1/ventas/cotizaciones/{id}/aceptar-cambios/`
  - Efecto: se **aplican** los cambios de la cotización al `Pedido` ya existente y la cotización vuelve a `Autorizada (3)` con `aprobado_snapshot` actualizado.
- Rechazar cambios:
  - **Endpoint**: `POST /api/v1/ventas/cotizaciones/{id}/rechazar-cambios/`
  - Efecto: se **revierte** la cotización al `aprobado_snapshot` y vuelve a `Autorizada (3)`; el `Pedido` no se modifica.

---

## 🔐 Seguridad y Reglas

- Acciones de mesa de control (autorizar/rechazar/aceptar-cambios/rechazar-cambios) requieren usuario con `is_superuser` o `is_admin_empresa`.  
- El vendedor puede crear y editar cotizaciones dentro de la ventana de tolerancia configurada; si excede, el backend rechaza la edición.

---

## ⚙️ Configuración de Tolerancia (ventana de edición)

- Variable: `COTIZACION_EDIT_WINDOW_MINUTES`
- Ubicación: [ERP/settings.py](file:///c:/Users/Jes%C3%BAs%20Ibarra/Desktop/django-backend-v2/ERP/settings.py)
- Default: `30` minutos. Se puede sobreescribir por entorno:

```bash
COTIZACION_EDIT_WINDOW_MINUTES=45
```

---

## ✅ Pruebas Internas (resumen)

- Crear cotización vía onboarding: crea en `cotizaciones` + `cotizacion_detalle` + `cotizacion_detalle_talla`; **no** crea pedido.
- Autorizar: crea `pedido` con folio y duplica el detalle, status `Autorizada (3)`.
- Rechazar: no crea pedido y no gasta folio.
- Solicitar cambios dentro de ventana: al re-enviar onboarding con `cotizacion_id`, cotización pasa a `Cambios Por Autorizar (5)`.
- Aceptar cambios: sincroniza `pedido` con el nuevo detalle; Rechazar cambios: restaura la cotización al `aprobado_snapshot` y no toca `pedido`.

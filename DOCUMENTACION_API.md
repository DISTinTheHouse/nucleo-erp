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

## 🔌 1.1 Integración Google (OAuth) — Gmail / Calendar (API)

Esta integración se hace **desde el backend** (para no exponer tokens). El frontend (Next.js) solo inicia el flujo y consume endpoints ya autenticados.

### Google Cloud Console (OAuth Client)

Crear un OAuth Client tipo **Web application** y configurar:

- **Orígenes autorizados de JavaScript**:
  - `https://lazzar-erp.vercel.app` (tu frontend)
  - `https://nucleo-erp.vercel.app` (tu backend)
- **URLs de redireccionamiento autorizadas**:
  - Producción: `https://nucleo-erp.vercel.app/api/v1/ai/google/oauth/callback/`
  - Desarrollo: `http://localhost:8003/api/v1/ai/google/oauth/callback/`

El backend solicita scopes para Drive (lectura), UserInfo (email), Gmail y Calendar.

### Flujo (Next.js)

1. **Iniciar conexión** (obtiene `auth_url`)

- **Endpoint**: `POST /api/v1/ai/google/oauth/connect/`
- **Body**:
  ```json
  {
    "next": "https://lazzar-erp.vercel.app/integraciones/google"
  }
  ```
- **Respuesta (200 OK)**:
  ```json
  {
    "ok": true,
    "provider": "google_drive",
    "auth_url": "https://accounts.google.com/o/oauth2/v2/auth?...",
    "redirect_uri": "https://nucleo-erp.vercel.app/api/v1/ai/google/oauth/callback/",
    "scope": "..."
  }
  ```

Notas:

- En el request de `connect` usar `credentials: "include"` (cookies JWT).
- Luego redirigir el navegador a `auth_url`.

2. **Callback** (lo ejecuta Google)

- **URL**: `GET /api/v1/ai/google/oauth/callback/`
- Google redirige a esta URL con `code` y `state`.
- El backend guarda tokens y finalmente redirige a `next` con query params:
  - `?ok=1&provider=google_drive` si todo salió bien
  - `?ok=0&error=...` si falló

Importante:

- Si el backend responde `invalid_state`, normalmente significa que el flujo **no se inició** con `POST /api/v1/ai/google/oauth/connect/` en el mismo navegador (o faltó `credentials: "include"` en el request).

3. **Consultar estado**

- **Endpoint**: `GET /api/v1/ai/google/oauth/status/`
- Útil para saber si ya está conectado y qué scopes tiene.

### Gmail (API)

Estos endpoints requieren que el usuario ya haya conectado Google con el flujo anterior.

- **Listar mensajes**: `GET /api/v1/ai/google/gmail/messages/?maxResults=20&q=in:inbox`
- **Detalle**: `GET /api/v1/ai/google/gmail/messages/{id}/`
- **Enviar**: `POST /api/v1/ai/google/gmail/send/`
  ```json
  { "to": "cliente@dominio.com", "subject": "Hola", "body": "Mensaje..." }
  ```

### Calendar (API)

- **Listar eventos**: `GET /api/v1/ai/google/calendar/events/`
- **Crear evento**: `POST /api/v1/ai/google/calendar/events/`

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
        "sku": "CAM-BAS-NEG-M",
        "tipo": "PT",
        "tipo_id": 2
      },
      "producto_variante": {
        "id": 15,
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
      "stock": 50,
      "cantidad": "50.0000",
      "fecha_actualizacion": "2026-06-27T09:00:00Z"
    }
  ]
  ```
- **Notas**:
  - `Existencia` soporta `producto` de forma directa.
  - `producto_variante` queda como opcional para escenarios donde sí aplique.
  - Las existencias se afectan por operaciones o recepciones, no por crear una orden de compra.

### Movimientos de Inventario

Historial operativo de entradas, salidas y ajustes.
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
      "fecha": "2026-06-27T14:30:00Z",
      "fecha_movimiento": "2026-06-27T14:30:00Z",
      "created_at": "2026-06-27T14:30:00Z",
      "usuario": 7,
      "usuario_nombre": "Usuario Demo",
      "almacen_id": 1,
      "sucursal_id": 5,
      "empresa_id": 1
    }
  ]
  ```
- **Detalle**: `GET /api/v1/inventarios/movimientos/{id}/detalles/`

- **Respuesta del detalle**:
  ```json
  {
    "id": 204,
    "tipo_movimiento": "ENTRADA",
    "fecha": "2026-06-27T14:30:00Z",
    "usuario": 7,
    "usuario_nombre": "Usuario Demo",
    "almacen_id": 1,
    "sucursal_id": 5,
    "empresa_id": 1,
    "detalle_count": 2,
    "detalle": [
      {
        "producto_id": 1,
        "producto_variante_id": null,
        "ubicacion_id": 10,
        "cantidad_before": "5.0000",
        "cantidad_after": "10.0000",
        "delta": "5.0000"
      }
    ],
    "antes_json": {
      "items": []
    },
    "despues_json": {
      "items": [
        {
          "producto_id": 1,
          "producto_variante_id": null,
          "ubicacion_id": 10,
          "cantidad_before": "5.0000",
          "cantidad_after": "10.0000",
          "delta": "5.0000"
        }
      ]
    }
  }
  ```

### Operaciones de Inventario

Operaciones oficiales del módulo. Este es el flujo recomendado para modificar existencias.
**Nota de Seguridad**: Requiere permisos de escritura y valida scope de empresa/sucursal.

- **Entrada**: `POST /api/v1/inventarios/operaciones/entrada`
- **Salida**: `POST /api/v1/inventarios/operaciones/salida`
- **Ajuste**: `POST /api/v1/inventarios/operaciones/ajuste`

- **Body base**:

  ```json
  {
    "almacen": 1,
    "observaciones": "Movimiento manual",
    "items": [
      {
        "producto": 1,
        "cantidad": "5.0000",
        "ubicacion": 10,
        "lote": null,
        "serie": null
      }
    ]
  }
  ```

- **Reglas**:
  - `ENTRADA`: suma cantidad.
  - `SALIDA`: resta cantidad y puede llegar a `0`, pero nunca a negativo.
  - `AJUSTE`: reemplaza la cantidad final por el valor enviado.
  - El backend registra auditoría y también persiste en `MovimientoInventario` y `MovimientoInventarioDetalle`.

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

- se crea/actualiza un registro en **Cotizaciones** con `estatus=Por Autorizar (2)`
- el detalle (productos/tallas/servicios por talla) se guarda en:
  - `CotizacionDetalle`: 1 registro por **producto**
    - `precio_lista`: snapshot del `Producto.precio_base` (referencia) al momento de cotizar
    - `precio_unitario`: precio editable (el vendedor puede ajustarlo y mesa de control valida)
  - `CotizacionDetalleTalla`: sub-líneas por **talla**:
    - `cantidad`
    - `lleva_bordado` + `bordado_config`
    - `lleva_serigrafia` + `serigrafia_config`
- los **servicios extras (ilimitados)** se guardan en:
  - `CotizacionServicioExtra`: (`nombre`, `monto`, `visible_en_factura`)
- **no** se crea `Pedido` ni se asigna folio `P-xxxxxx` hasta que **mesa de control autorice**

### 0) Dashboard: listar y ver cotizaciones

- **Listar (tabla)**: `GET /api/v1/ventas/cotizaciones/`
- **Scope**:
  - Vendedor (usuario normal): solo sus cotizaciones (`vendedor = request.user`)
  - Mesa de control (`is_admin_empresa`) / `is_superuser`: todas las cotizaciones de la empresa
- **Query Params (opcionales)**:
  - `q`: busca por `oc`, `cliente.nombre`, `cliente.razon_social`, `cliente.rfc` o `id` (si es numérico)
  - `estatus`: uno o varios separados por coma (ej: `2` o `2,5`)
  - `ordering`: lista separada por coma. Permitidos: `id`, `created_at`, `updated_at`, `gran_total`, `estatus` (ej: `-created_at`)
- **Campos útiles para tabla**:
  - `estatus_label`, `cliente_nombre`, `cliente_razon_social`, `pedido_id`, `pedido_folio`
  - `piezas`: sumatoria de `cantidad` en tallas (detalle de la cotización)
  - `importe_sin_iva`: importe antes de IVA (calculado a partir de `gran_total` e `iva`)

- **Ver cotización completa (modal)**: `GET /api/v1/ventas/cotizaciones/{id}/`
  - Incluye campo `detalles` (productos + tallas + bordado_config), `estatus_label`, `piezas` e `importe_sin_iva`.

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

### 2) Crear cotización (con detalle)

- **Endpoint**: `POST /api/v1/ventas/cotizaciones/onboarding/`

**Reglas del flujo**

- El backend crea **1 `CotizacionDetalle` por producto** (aunque se repita el producto en el payload).
- Las tallas repetidas se consolidan sumando `cantidad`.
- Si una talla viene con `lleva_bordado=true`, entonces `bordado_config` es requerido y se guarda en `CotizacionDetalleTalla.bordado_config`.
- Si una talla viene con `lleva_serigrafia=true`, entonces `serigrafia_config` es requerido y se guarda en `CotizacionDetalleTalla.serigrafia_config`.
- `precio_unitario` es editable por el vendedor; `precio_lista` queda como referencia del precio base al momento de cotizar.
- `servicios_extras` es opcional y permite agregar cargos ilimitados con control de visibilidad en factura (`visible_en_factura`).
- La cotización queda en `estatus=Por Autorizar (2)` para que mesa de control valide.

**Folio de Pedido**

- El folio `P-xxxxxx` se asigna **solo** cuando mesa de control autoriza (`POST /api/v1/ventas/cotizaciones/{id}/autorizar/`).

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
      "precio_unitario": "250.00",
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
        {
          "talla": 2,
          "cantidad": 4,
          "lleva_bordado": false,
          "lleva_serigrafia": true,
          "serigrafia_config": {
            "ubicacion": "PECHO",
            "tintas": 1,
            "notas": "Serigrafía 1 tinta"
          }
        }
      ]
    }
  ],
  "servicios_extras": [
    {
      "nombre": "Serigrafía (cargo global)",
      "monto": "1500.00",
      "visible_en_factura": false
    },
    { "nombre": "Envío express", "monto": "250.00", "visible_en_factura": true }
  ]
}
```

**Respuesta**

```json
{
  "cotizacion": { "id": 10 },
  "detalles": [
    {
      "id": 555,
      "cotizacion": 10,
      "producto": 50,
      "precio_lista": "300.00",
      "precio_unitario": "100.00",
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
  ],
  "servicios_extras": [
    {
      "id": 1,
      "nombre": "Serigrafía (cargo global)",
      "monto": "1500.00",
      "visible_en_factura": false
    }
  ]
}
```

### 3) Edición con ventana de tiempo + notificación a mesa de control

- **Endpoint**: `PATCH /api/v1/ventas/cotizaciones/{id}/`
- Regla: la edición está permitida dentro del periodo configurado. Si la cotización ya estaba `Autorizada (3)`, al editar pasa a `Cambios Por Autorizar (5)` y mesa de control debe decidir.
- La edición **no** modifica el `Pedido` automáticamente. El `Pedido` solo se actualiza si mesa de control ejecuta `aceptar-cambios`.
- Para editar también el detalle (productos/tallas/bordado), re-envía `POST /api/v1/ventas/cotizaciones/onboarding/` agregando `cotizacion_id` (y el detalle completo actualizado).

---

## 📦 11. Pedidos

Gestión de pedidos generados a partir de cotizaciones autorizadas.

#### Automatización de Órdenes de Trabajo (Producción)

Al autorizar una cotización (`/autorizar/`), el backend genera automáticamente las siguientes órdenes de trabajo según la configuración de los productos:

1.  **Orden de Producción (OP)**: Se genera siempre por cada pedido autorizado.
    - Nomenclatura: `OP-P-XXXX`
2.  **Orden de Bordado (OB)**: Se genera si algún producto tiene `lleva_bordado: true`.
    - Nomenclatura: `OB-P-XXXX`
3.  **Orden de Reflejante (OR)**: Se genera si algún producto tiene `lleva_reflejante: true`.
    - Nomenclatura: `OR-P-XXXX`
4.  **Orden de Corte de Manga (OCM)**: Se genera si algún producto tiene `lleva_corte_manga: true`.
    - Nomenclatura: `OCM-P-XXXX`

Estas órdenes nacen en estado **PENDIENTE** y quedan vinculadas al pedido original.

- **Listar**: `GET /api/v1/ventas/pedidos/`

---

## 🧮 Mesa de Control

- Ver cotizaciones pendientes: filtra por `estatus=2 (Por Autorizar)` y `estatus=5 (Cambios Por Autorizar)`.
- Autorizar cotización:
  - **Endpoint**: `POST /api/v1/ventas/cotizaciones/{id}/autorizar/`
  - Efecto: se **duplica** la cotización a `Pedido` con folio `P-xxxxxx`:
    - detalle (productos/tallas) + precios snapshot (`precio_lista` / `precio_unitario`)
    - servicios por talla (bordado/serigrafía + configs)
    - servicios extras ilimitados (`servicios_extras`)
    - se marca la cotización como `Autorizada (3)` y se guarda un `aprobado_snapshot` del estado aprobado.
- Rechazar cotización:
  - **Endpoint**: `POST /api/v1/ventas/cotizaciones/{id}/rechazar/`
  - Efecto: la cotización pasa a `Rechazada (4)`. **No** se crea pedido ni se gasta folio.
- Aceptar cambios:
  - **Endpoint**: `POST /api/v1/ventas/cotizaciones/{id}/aceptar-cambios/`
  - Efecto: se **aplican** los cambios de la cotización al `Pedido` ya existente (detalle + `servicios_extras`) y la cotización vuelve a `Autorizada (3)` con `aprobado_snapshot` actualizado.
- Rechazar cambios:
  - **Endpoint**: `POST /api/v1/ventas/cotizaciones/{id}/rechazar-cambios/`
  - Efecto: se **revierte** la cotización al `aprobado_snapshot` (incluye detalle y `servicios_extras`) y vuelve a `Autorizada (3)`; el `Pedido` no se modifica.

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

---

## 🧾 Compras - Recepciones (Onboarding)

**Base URL**: `/api/v1/compras/`

La recepción es el proceso unificado que afecta existencias tanto para órdenes de compra (`OC`) como para órdenes de producción (`OP`).

### 1) Obtener datos para el formulario

- **Endpoint**: `GET /api/v1/compras/recepciones/onboarding/`
- **Query Params (opcionales)**:
  - `orden_compra_id`: si se envía, carga esa orden; si no, el backend puede seleccionar una disponible.
- **Respuesta (resumen)**:
  ```json
  {
    "catalogos": {
      "almacenes": [],
      "ubicaciones": [],
      "series_recepcion": [
        { "id_serie_folio": 1, "tipo_documento": "RECEPCION", "serie": "RC", "sucursal_id": 2 }
      ]
    },
    "busqueda": {
      "ordenes_compra": [
        {
          "id": 112,
          "folio": "OC-000112",
          "detalle": [
            {
              "id": 25,
              "producto_id": 1,
              "producto_nombre": "Tela gabardina",
              "cantidad_ordenada": "10.0000",
              "cantidad_recibida": "4.0000",
              "cantidad_pendiente": "6.0000"
            }
          ]
        }
      ],
      "ordenes_produccion": [
        {
          "id": 88,
          "folio": "OP-000088",
          "cerrar_orden": true,
          "detalle": [
            {
              "id": 14,
              "producto_id": 9,
              "producto_variante_id": 31,
              "producto_nombre": "Playera Negra M",
              "cantidad_ordenada": "5.00",
              "cantidad_recibida": "2.0000",
              "cantidad_pendiente": "3.0000"
            }
          ]
        }
      ]
    }
  }
  ```

### 2) Registrar recepción

- **Endpoint**: `POST /api/v1/compras/recepciones/onboarding/`

**Reglas del flujo**

- La recepción puede ser total o parcial.
- Debe enviarse exactamente un origen: `orden_compra` o `orden_produccion`.
- Para `OC`, el backend toma el `producto` desde `OrdenCompraDetalle`.
- Para `OP`, el backend toma `producto` y `producto_variante` desde `OrdenProduccionDetalle`.
- Si el almacén requiere ubicación, `ubicacion` es obligatoria.
- Ni la orden de compra ni la orden de producción mueven inventario por sí mismas; la recepción sí.
- La recepción genera folio con series como `RC`, `RT` o `RZ`.
- Además de afectar `Existencia`, el backend genera auditoría y movimientos formales de inventario.
- Si la recepción viene de producción, `MovimientoInventario.op` queda ligado a la `OP`.
- El flujo de recepción centraliza la entrada de inventario; `ProductoTerminadoEntradas` queda redundante para este caso de uso.

**Body (ejemplo)**

```json
{
  "recepcion": {
    "orden_compra": 112,
    "almacen": 8,
    "serie_codigo": "RC",
    "fecha_recepcion": "2026-06-16T21:16:14.968Z",
    "remision": "R-01",
    "factura_referencia": "F-01",
    "observaciones": "",
    "transportista": null
  },
  "detalle": [
    {
      "orden_compra_detalle": 25,
      "cantidad_recibida": "1"
    },
    {
      "orden_compra_detalle": 26,
      "cantidad_recibida": "1",
      "ubicacion": 12
    }
  ]
}
```

**Body (ejemplo OP)**

```json
{
  "recepcion": {
    "orden_produccion": 88,
    "almacen": 8,
    "serie_codigo": "RC",
    "observaciones": "Entrada de producto terminado desde OP"
  },
  "detalle": [
    {
      "orden_produccion_detalle": 14,
      "cantidad_recibida": "4.0000"
    }
  ]
}
```

**Respuesta (resumen)**

```json
{
  "recepcion": {
    "id": 33,
    "folio": "RC-000033",
    "estatus": 2
  },
  "detalle": [
    {
      "id": 101,
      "recepcion": 33,
      "orden_compra_detalle": 25,
      "orden_produccion_detalle": null,
      "producto": 1,
      "producto_variante": null,
      "ubicacion": null,
      "lote": null,
      "serie": null,
      "cantidad_recibida": "1.0000"
    }
  ],
  "movimiento_id": 450,
  "movimiento_inventario_id": 77
}
```

---

## 🏭 Producción - Lista de Materiales (BOM)

**Base URL**: `/api/v1/produccion/`

### 1) Listar BOM

- **Endpoint**: `GET /api/v1/produccion/lista-material/`
- **Query Params (opcionales)**:
  - `producto_variante_id`

### 2) Consulta masiva de BOM

- **Endpoint**: `GET /api/v1/produccion/lista-material/bulk/?producto_variante_ids=1,2,3`

### 3) Crear BOM

- **Endpoint**: `POST /api/v1/produccion/lista-material/`

```json
{
  "empresa": 1,
  "producto_variante": 15,
  "version": 1,
  "observaciones": "BOM inicial",
  "materia_prima_detalle": [
    {
      "componente": 101,
      "cantidad": "2.50",
      "unidad": 1,
      "desperdicio": "0.00",
      "obligatorio": true,
      "observaciones": ""
    }
  ]
}
```

### 4) Editar BOM

- **Endpoint**: `PUT /api/v1/produccion/lista-material/{bom_id}/`
- **Endpoint**: `PATCH /api/v1/produccion/lista-material/{bom_id}/`

**Notas**

- Si el request incluye `materia_prima_detalle`, el backend reemplaza el detalle actual por el nuevo arreglo enviado.
- Si en `PATCH` no se envía `materia_prima_detalle`, se conserva el detalle existente.

### 5) Orden de Producción (Onboarding)

- **Endpoint**: `GET|POST /api/v1/produccion/orden-produccion/onboarding/`
- **Regla**:
  - El frontend no necesita enviar `bom` dentro de cada detalle.
  - El backend resuelve automáticamente el BOM activo a partir de `producto_variante`.
  - El contrato para frontend se mantiene: misma URL y mismo body base para crear la OP.

### 6) Crear Orden de Producción con Consumo Automático

- **Endpoint**: `POST /api/v1/produccion/orden-produccion/onboarding/`
- **Compatibilidad**:
  - No requiere cambios del frontend si ya consumía el onboarding de producción.
  - El backend sigue resolviendo el BOM internamente.
  - El descuento de inventario sucede automáticamente al confirmar la OP.

**Reglas del flujo**

- Se valida que cada `producto_variante` tenga un BOM activo en la empresa del usuario.
- Se calculan los insumos requeridos tomando `BomDetalle.cantidad * cantidad_op`.
- Si el BOM tiene `desperdicio`, se aplica al cálculo del consumo.
- Antes de crear definitivamente la OP, el backend valida existencias suficientes de cada insumo.
- Si hay inventario suficiente:
  - crea `OrdenProduccion`
  - crea `OrdenProduccionDetalle`
  - descuenta `Existencia`
  - registra `ConsumoProduccion` y `consumo_detalle`
  - registra `MovimientoInventario` y `MovimientoInventarioDetalle`
  - registra `AuditoriaEvento`
- Si no hay inventario suficiente o el BOM está incompleto, responde `400` y no confirma la operación.

**Body (sin cambios para frontend)**

```json
{
  "empresa": 1,
  "sucursal": 1,
  "prioridad": 1,
  "observaciones": "OP de prueba",
  "orden_produccion_detalle": [
    {
      "producto_variante_id": 15,
      "cantidad": "3.0000",
      "unidad": 1,
      "observaciones": ""
    }
  ]
}
```

**Respuesta (resumen)**

```json
{
  "msg": "Orden de producción creada exitosamente",
  "op_id": 10,
  "folio_op": "OP-000010",
  "consumo_produccion_id": 3,
  "movimiento_inventario_id": 25,
  "movimiento_id": 901
}
```

---

## 🤖 Asistente IA (Chat)

Asistente conversacional para ejecutar consultas y acciones controladas desde el frontend (próxima integración en Next.js).

- **Endpoint**: `POST /api/v1/ai/chat/`
- **Autenticación**: sesión/Token del usuario (hereda permisos).
- **Headers**: `Content-Type: application/json`

### Request

```json
{
  "message": "¿Cuántas empresas tengo?",
  "conversation": [
    { "role": "user", "content": "Hola" },
    { "role": "assistant", "content": "¿En qué te ayudo?" }
  ]
}
```

Notas:

- `message` es obligatorio.
- `conversation` es opcional; enviar historial breve mejora el contexto (máx. ~20 turnos recientes).

### Response

```json
{
  "reply": "Tienes 1 empresa.",
  "tool_results": [
    {
      "name": "count_empresas",
      "args": {},
      "result": { "ok": true, "count": 1 }
    }
  ]
}
```

### Consultas soportadas

- Conteos: “¿Cuántas empresas/usuarios/cotizaciones hay?”
- Listados: “Lista las 5 empresas”, “Muéstrame 10 usuarios de mi empresa”
- Búsquedas: “Busca clientes con RFC XAXX010101000”
- Detalles: “Dame los datos de la empresa lazzar-mex-0001”
- Permisos: “¿Qué permisos efectivos tengo?”

### Acciones (crear)

- Empresa (solo superuser): “Crea una empresa …”
- Rol (solo superuser): “Crea un rol Ventas …”
- Usuario (admin-empresa o superuser): “Crea un usuario maria.garcia con rol Ventas …”
- Cliente (admin-empresa o superuser): “Crea un cliente ‘Comercial XYZ’ con RFC XAXX010101000”

El asistente valida campos críticos (RFC, SAT) y solicitará datos faltantes.

### Seguridad

- Respeta permisos del usuario autenticado:
  - Superuser: puede crear Empresas y Roles; también Usuarios y Clientes.
  - Admin de empresa: puede crear Usuarios y Clientes en su empresa.
  - Usuario normal: consultas; no crea.
- Si faltan permisos o datos, el asistente lo indicará sin ejecutar acciones.

### Notas de configuración

- Variables de entorno:
  - `OPENAI_API_KEY` (obligatoria)
  - `OPENAI_MODEL` (opcional, por defecto `gpt-4o-mini`)
  - `OPENAI_BASE_URL` (opcional)
- Archivos relevantes:
  - Endpoint DRF: `ia/api/urls.py`, `ia/api/views.py`
  - Configuración: `ERP/settings.py`

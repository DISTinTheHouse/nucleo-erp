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

- **Reporte de cierre por periodo**: `GET /api/v1/inventarios/existencias/reporte-existencias-periodo/`
- **Uso esperado en frontend**:
  - Seleccionar `fecha_inicio`
  - Seleccionar `fecha_final`
  - Seleccionar `almacen_id`
  - Clic en generar reporte
- **Query params mínimos**:
  - `fecha_inicio`: requerido, formato `YYYY-MM-DD`
  - `fecha_final`: requerido, formato `YYYY-MM-DD`
  - `almacen_id`: opcional si el usuario quiere un almacén específico; si se omite, regresa todos los almacenes visibles para el usuario
- **Paginación**: aplica únicamente sobre `results` (el detalle por almacén/producto/variante). `fecha_inicio`, `fecha_final`, `filtros`, `resumen` y `resumen_por_almacen` son agregados de todo el periodo/consulta, no solo de la página actual.
  - `page`: opcional, número de página (default `1`)
  - `page_size`: opcional, tamaño de página (default `200`, máximo `2000`)
- **Fórmula de cierre**:
  - `existencia_final = existencia_inicial + entradas - salidas`
  - `costo_total_existencia_final = sum(existencia_final * costo_unitario_final)`
  - En la respuesta, `salidas` se entrega como valor positivo para facilitar la lectura del reporte.
- **Ejemplo**:
  - `GET /api/v1/inventarios/existencias/reporte-existencias-periodo/?fecha_inicio=2026-07-01&fecha_final=2026-07-31&almacen_id=1&page=1&page_size=200`
- **Respuesta**:

  ```json
  {
    "count": 450,
    "next": "https://.../reporte-existencias-periodo/?almacen_id=1&fecha_final=2026-07-31&fecha_inicio=2026-07-01&page=2",
    "previous": null,
    "results": [
      {
        "almacen_id": 1,
        "almacen_codigo": "PT-MTY",
        "almacen_nombre": "Almacén PT Monterrey",
        "producto_id": 8275,
        "producto_variante_id": 2284,
        "producto_nombre": "GORRA CAZADOR",
        "sku": "GOR-CAZ-NEG-UNI",
        "color": "NEGRO",
        "talla": "UNITALLA",
        "existencia_inicial": "10.0000",
        "entradas": "5.0000",
        "salidas": "2.0000",
        "existencia_final": "13.0000",
        "costo_unitario_final": "140.00",
        "costo_existencia_final": "1820.00"
      }
    ],
    "fecha_inicio": "2026-07-01",
    "fecha_final": "2026-07-31",
    "filtros": {
      "almacen_id": 1
    },
    "resumen": {
      "existencia_inicial": "120.0000",
      "entradas": "35.0000",
      "salidas": "20.0000",
      "existencia_final": "135.0000",
      "costo_total_existencia_final": "18900.00"
    },
    "resumen_por_almacen": [
      {
        "almacen_id": 1,
        "almacen_codigo": "PT-MTY",
        "almacen_nombre": "Almacén PT Monterrey",
        "existencia_inicial": "120.0000",
        "entradas": "35.0000",
        "salidas": "20.0000",
        "existencia_final": "135.0000",
        "costo_total_existencia_final": "18900.00"
      }
    ]
  }
  ```

  - `count`/`next`/`previous`/`results` son la paginación estándar de DRF sobre el arreglo de detalle (antes expuesto como `detalle`).
  - `fecha_inicio`, `fecha_final`, `filtros`, `resumen` y `resumen_por_almacen` se mantienen igual que antes de paginar: reflejan todo el resultado filtrado, no cambian entre páginas.

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

- **Reporte por tipo y rango**: `GET /api/v1/inventarios/movimientos/reporte-movimientos-periodo/`
- **Uso esperado en frontend**:
  - Seleccionar `tipo_movimiento`
  - Seleccionar `fecha_inicio`
  - Seleccionar `fecha_final`
  - Seleccionar `almacen_id` si se quiere filtrar un almacén específico
  - O usar la opción `TODOS LOS ALMACENES` dejando `almacen_id` vacío o sin enviarlo
  - Clic en buscar
  - Con la misma respuesta generar PDF
- **Query params**:
  - `tipo_movimiento`: requerido, `ENTRADA`, `SALIDA`, `AJUSTE` o `TRANSFERENCIA`
  - `fecha_inicio`: requerido, formato `YYYY-MM-DD`
  - `fecha_final`: requerido, formato `YYYY-MM-DD`
  - `almacen_id`: opcional; si no se envía, el reporte incluye todos los almacenes visibles para el usuario
- **Ejemplo**:
  - `GET /api/v1/inventarios/movimientos/reporte-movimientos-periodo/?tipo_movimiento=SALIDA&fecha_inicio=2026-07-01&fecha_final=2026-07-31&almacen_id=1`
  - `GET /api/v1/inventarios/movimientos/reporte-movimientos-periodo/?tipo_movimiento=SALIDA&fecha_inicio=2026-07-01&fecha_final=2026-07-31`
  - `GET /api/v1/inventarios/movimientos/reporte-movimientos-periodo/?tipo_movimiento=TRANSFERENCIA&fecha_inicio=2026-07-01&fecha_final=2026-07-31`
- **Nota TRANSFERENCIA**:
  - Cada renglón incluye `transferencia_id` y `transferencia_folio`.
  - El reporte incluye la transferencia si el usuario tiene acceso al almacén de origen o al de destino.
  - Al crear una transferencia (WMS) se registra automáticamente `AuditoriaEvento` (`modulo=inventarios`, `accion=TRANSFERENCIA`, `tabla=existencias`), por lo que también aparece en el listado general de movimientos (`GET /api/v1/inventarios/movimientos/`).
- **Respuesta**:
  ```json
  {
    "tipo_movimiento": "SALIDA",
    "fecha_inicio": "2026-07-01",
    "fecha_final": "2026-07-31",
    "filtros": {
      "almacen_id": 1
    },
    "resumen": {
      "total_movimientos": 12,
      "total_registros": 18,
      "total_cantidad": "85.0000"
    },
    "resultados": [
      {
        "movimiento_inventario_id": 245,
        "movimiento_detalle_id": 810,
        "tipo_movimiento": "SALIDA",
        "fecha_movimiento": "2026-07-12T16:40:00Z",
        "almacen_id": 1,
        "almacen_codigo": "PT-MTY",
        "almacen_nombre": "Almacén PT Monterrey",
        "ubicacion_id": 5,
        "ubicacion_nombre": "Almacén PT Monterrey - A-1-1-1",
        "producto_id": 8275,
        "producto_variante_id": 2284,
        "sku": "GOR-CAZ-NEG-UNI",
        "producto_nombre": "GORRA CAZADOR - NEGRO - UNITALLA",
        "producto_base_nombre": "GORRA CAZADOR",
        "color": "NEGRO",
        "talla": "UNITALLA",
        "cantidad": "10.0000",
        "costo_unitario": "140.00",
        "costo_total": "1400.00",
        "pedido_id": 49,
        "pedido_folio": "PED-49",
        "recepcion_id": null,
        "ajuste_inventario_id": null,
        "op_id": null,
        "usuario_id": 3,
        "usuario_nombre": "Desarrollo",
        "observaciones": "Salida por pedido autorizado",
        "comentarios": "Salida por pedido autorizado",
        "motivo_ajuste": null
      }
    ]
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
- **Orden**: fijo, más reciente primero (`created_at` descendente, desempate por `id` descendente). No es configurable: el parámetro `ordering` ya no se admite y se ignora si se envía.
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

> 🚨 **Decisión de negocio (Presidencia) — v2 en producción**
>
> Al autorizar una cotización el backend **NO genera automáticamente** órdenes de trabajo. La sección anterior (OP / OB / OR / OCM automáticas) quedó **deshabilitada** por decisión de Presidencia y el código está preservado comentado para uso futuro.
>
> El flujo actual es manual / onboarding por módulo:
>
> - La cotización autorizada se convierte en **`Pedido`** con folio `P-xxxxxx`.
> - Las órdenes de trabajo se crean **desde los endpoints onboarding de Producción**:
>   - `POST /api/v1/produccion/orden-bordado/onboarding/` para bordado
>   - Endpoints equivalentes para reflejante / corte de manga cuando estén disponibles.
> - Para surtido de almacén se usa el flujo tradicional `WMS → Picking` (documento-only, sin transferencias ni OT automáticas).

- **Listar**: `GET /api/v1/ventas/pedidos/`
- **Detalle**: `GET /api/v1/ventas/pedidos/{id}/`
  - Campos de contabilidad (dinero / forma*pago / metodos_pago / uso_cfdi / subtotal / iva / gran_total / precios_unitarios*$\_en_detalles_tallas_servicios):
    - ✅ Se envían **completos** si el usuario tiene rol `Mesa-de-Control` o `Ventas` (o es `is_superuser` / `is_admin_empresa`).
    - ❌ Se **eliminan del JSON** (no vienen en el response) para roles `WMS`, `Compras` o cualquier otro.
  - **Nuevo**: `documentos[]` — lista de todos los documentos ligados al pedido (1 solo query por relación, prefetch optimizado).
    - **Shape de cada entrada**:
      ```ts
      type PedidoDocumento = {
        id: number;
        tipo:
          | "cotizacion"
          | "orden_compra"
          | "factura"
          | "orden_produccion"
          | "orden_bordado"
          | "orden_reflejante"
          | "orden_corte_manga"
          | "picking"
          | "packing"
          | "envio"
          | "entrega"
          | "devolucion"
          | "movimiento_inventario";
        label: string; // nombre legible del tipo ("Cotización", "Picking (WMS)", ...)
        folio: string; // folio natural del documento; si no tiene, cae a `id`
        fecha: string | null; // ISO string (date o datetime) del campo fecha/creación; null si no hay
        estatus: string | number | null; // label legible si hay choices (ex. "Autorizada"), null si el modelo no tiene estatus
      };
      ```
    - **Tipos registrados hasta ahora** (editar en `ventas/services/pedido_documentos_service.py → DOCUMENTOS_CONFIG` si hace falta agregar/quitar alguno):
      - `cotizacion` → Cotización origen (0 o 1)
      - `orden_compra` → Ordenes de compra vinculadas (0..N)
      - `factura` → Facturas (0..N)
      - `orden_produccion` → OP (0..N)
      - `orden_bordado`, `orden_reflejante`, `orden_corte_manga` → OTs (0..N cada una)
      - `picking`, `packing` → WMS (0..N cada una)
      - `envio`, `entrega`, `devolucion` → Logística (0..N cada una)
      - `movimiento_inventario` → MovInv ligados (0..N)
    - **Ejemplo de response**:
      ```json
      "documentos": [
        { "id": 52,  "tipo": "cotizacion", "label": "Cotización",         "folio": "COT-2025-0042", "fecha": "2025-06-18T09:12:00",  "estatus": "Confirmada" },
        { "id": 301, "tipo": "orden_compra", "label": "Orden de Compra",   "folio": "OC-0871",       "fecha": "2025-06-19",           "estatus": "Autorizada" },
        { "id": 990, "tipo": "orden_produccion", "label": "Orden de Producción", "folio": "OP-771",   "fecha": "2025-06-21T10:00:00",  "estatus": "En produccion" },
        { "id": 556, "tipo": "picking", "label": "Picking (WMS)",          "folio": "PK-2025-0112",  "fecha": "2025-07-05T09:00:00",  "estatus": "Surtida" }
      ]
      ```
  - Consumo en Next.js:

    ```ts
    // El usuario ya tiene sesión activa (cookie de Django).
    type PedidoDocumento = {
      id: number;
      tipo:
        | "cotizacion"
        | "orden_compra"
        | "factura"
        | "orden_produccion"
        | "orden_bordado"
        | "orden_reflejante"
        | "orden_corte_manga"
        | "picking"
        | "packing"
        | "envio"
        | "entrega"
        | "devolucion"
        | "movimiento_inventario";
      label: string;
      folio: string;
      fecha: string | null;
      estatus: string | number | null;
    };

    type Pedido = {
      id: number;
      folio?: string;
      gran_total?: string | number;
      documentos: PedidoDocumento[];
      // ... resto de campos ya conocidos
      detalles?: any[];
      servicios_extras?: any[];
    };

    // Fetch
    async function fetchPedido(id: number | string): Promise<Pedido> {
      const res = await fetch(`/api/v1/ventas/pedidos/${id}/`);
      if (!res.ok) throw new Error("Error cargando pedido");
      return res.json();
    }

    // En la UI, leer siempre con optional chaining:
    const granTotal = pedido?.gran_total ?? 0; // no romper en WMS/Compras
    const servicioMonto = pedido?.servicios_extras?.[0]?.monto ?? 0;

    // Renderizar lista de documentos con botón "Ver" que abra modal (endpoint de detalle por tipo se agrega después)
    // pedido.documentos.map(d => <li key={`${d.tipo}-${d.id}`}>{d.label} · {d.folio}</li>)

    // Para abrir "Ver detalles" del pedido en pestaña nueva:
    //   window.open(`/pedidos/${pedido.id}`, "_blank");
    //   o <Link href={`/pedidos/${pedido.id}`} target="_blank">Ver detalles</Link>
    ```

    - No asumas que existen los keys $$; usa `pedido?.campo ?? fallback` para no romper en WMS/Compras.
    - Para abrir en pestaña nueva desde "Ver detalles": `window.open(\`/pedidos/\${id}\`, "\_blank")`(o`next/navigation`+`<Link target="_blank">`).

---

## 🧮 Mesa de Control

- Ver cotizaciones pendientes: filtra por `estatus=2 (Por Autorizar)` y `estatus=5 (Cambios Por Autorizar)`.
- Autorizar cotización:
  - **Endpoint**: `POST /api/v1/ventas/cotizaciones/{id}/autorizar/`
  - Efecto: se **duplica** la cotización a `Pedido` con folio `P-xxxxxx`:
    - detalle (productos/tallas) + precios snapshot (`precio_lista` / `precio_unitario`)
    - servicios por talla (bordado/serigrafía + configs)
    - servicios extras ilimitados (`servicios_extras`)
    - se descuenta inventario de las existencias de la misma empresa/sucursal según los productos y variantes del pedido
    - se registra `MovimientoInventario` tipo `SALIDA` ligado al `pedido` y su `AuditoriaEvento`
    - se marca la cotización como `Autorizada (3)` y se guarda un `aprobado_snapshot` del estado aprobado.
  - Regla crítica: **NO se generan órdenes de trabajo automáticamente** (OB/OR/OP/OCM). Las OT se crean de forma manual a través de los endpoints onboarding de Producción / WMS.
  - Defaults de facturación: si `cotizacion.persona_pagos / correo_facturas / telefono_pagos / forma_pago / metodo_pago / uso_cfdi` vienen `null`, el backend usa sensible defaults derivados del cliente (`razon_social`/`email`/`telefono`) y los TextChoices de `Cotizacion.FormaPago.TRANSFERENCIA`, `MetodoPago.PUE`, `UsoCfdi.G03`. Esto garantiza que la conversión `Cotizacion → Pedido` nunca falle por campos `NOT NULL` de facturación.
- Rechazar cotización:
  - **Endpoint**: `POST /api/v1/ventas/cotizaciones/{id}/rechazar/`
  - Efecto: la cotización pasa a `Rechazada (4)`. **No** se crea pedido ni se gasta folio.
- Aceptar cambios:
  - **Endpoint**: `POST /api/v1/ventas/cotizaciones/{id}/aceptar-cambios/`
  - Efecto: se **aplican** los cambios de la cotización al `Pedido` ya existente (detalle + `servicios_extras`) y la cotización vuelve a `Autorizada (3)` con `aprobado_snapshot` actualizado.
  - Si cambian cantidades:
    - el backend descuenta inventario adicional cuando el nuevo pedido aumenta cantidades
    - el backend regresa inventario cuando el nuevo pedido reduce cantidades
    - cada ajuste genera su `MovimientoInventario` (`SALIDA` o `ENTRADA`) y auditoría correspondiente
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

## 🧾 Compras - Órdenes de Compra

**Base URL**: `/api/v1/compras/`

### Ver una orden de compra con recepciones relacionadas

- **Endpoint**: `GET /api/v1/compras/ordenes/{id}/`
- **Respuesta**:
  - `detalles` (renglones de la OC) y `recepciones[]` anidadas completas (con su `detalles[]`) se conservan tal cual.
  - **Nuevo** `pedido_vinculado: {id, folio} | null` — el FK `pedido` del modelo permite null.
  - **Nuevo** `documentos[]` — lista plana de documentos ligados a la OC (para FE abrir modal por `tipo + id`). Shape:

    ```ts
    { id: number, tipo: "pedido" | "solicitud_compra" | "recepcion" | "factura_proveedor" | "movimiento_inventario",
      label: string, folio: string, fecha: string | null, estatus: string | number | null }
    ```

    - `movimiento_inventario` se arma via `recepcion_set.movimientoinventario_set` (sin duplicados).

- **Visibilidad de campos $$ (estatus/subtotal/impuestos/gran_total y $ en detalles)**:
  - Se envía todo intacto si `is_superuser` / `is_admin_empresa` o alguno de estos roles: `Mesa-de-Control`, `Ventas`, `Compras`, `Contabilidad`, `ContaVentas`, `ContaCompras`, `MesaControlYVentas`.
  - Cualquier otro rol (incl. WMS sin compras): se eliminan los keys $$ del response.
- Consumo FE: usa optional chaining `oc?.gran_total ?? 0` y `oc?.detalles?.[0]?.precio ?? 0` para no romper.

**Respuesta (resumen de los campos nuevos)**

```json
{
  "id": 112,
  "folio": "OC-000112",
  "estatus": 4,
  "estatus_label": "Parcialmente recibida",
  "pedido_vinculado": { "id": 219, "folio": "PD-000219" },
  "documentos": [
    {
      "id": 219,
      "tipo": "pedido",
      "label": "Pedido",
      "folio": "PD-000219",
      "fecha": "2026-06-12T09:00:00+00:00",
      "estatus": "En produccion"
    },
    {
      "id": 44,
      "tipo": "solicitud_compra",
      "label": "Solicitud de Compra",
      "folio": "44",
      "fecha": null,
      "estatus": null
    },
    {
      "id": 35,
      "tipo": "recepcion",
      "label": "Recepción",
      "folio": "RC-000035",
      "fecha": "2026-07-07T12:00:00Z",
      "estatus": "Recibida"
    },
    {
      "id": 7,
      "tipo": "factura_proveedor",
      "label": "Factura Proveedor",
      "folio": "F-PROV-9021",
      "fecha": "2026-07-08",
      "estatus": "Registrada"
    },
    {
      "id": 5512,
      "tipo": "movimiento_inventario",
      "label": "Movimiento Inventario",
      "folio": "5512",
      "fecha": "2026-07-07T13:10:00Z",
      "estatus": null
    }
  ],
  "detalles": [],
  "recepciones": []
}
```

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
        {
          "id_serie_folio": 1,
          "tipo_documento": "RECEPCION",
          "serie": "RC",
          "sucursal_id": 2
        }
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

## 💰 Finanzas - Registrar Factura Pendiente por Cobrar

**Base URL**: `/api/v1/finanzas/`

Endpoint directo para registrar una factura manual pendiente de cobro para un cliente y crear automáticamente su cuenta por cobrar y su póliza contable con detalle.

### 1) Registrar factura pendiente

- **Endpoint**: `POST /api/v1/finanzas/facturas/registrar-pendiente-cobro/`
- **Uso**: el frontend envía la información base de la factura; el backend valida, genera la factura y deja creada la cuenta por cobrar.

**Body (ejemplo)**

```json
{
  "cliente": 15,
  "moneda": 1,
  "pedido": null,
  "folio": "FAC-PEND-001",
  "fecha_vencimiento": "2026-07-31",
  "subtotal": "1000.00",
  "descuento": "0.00",
  "impuestos": "160.00",
  "total": "1160.00",
  "referencia": "PENDIENTE-JULIO",
  "observaciones": "Factura pendiente por cobrar registrada manualmente"
}
```

**Campos**

- `cliente`: obligatorio, debe pertenecer a la empresa del usuario.
- `moneda`: obligatorio, debe estar activa y disponible para la empresa.
- `pedido`: opcional; si se envía, debe pertenecer a la empresa y coincidir con el mismo cliente y moneda.
- `folio`: opcional; si no se envía, el backend genera uno automáticamente.
- `fecha_vencimiento`: opcional.
- `subtotal`: obligatorio.
- `descuento`: opcional, default `0.00`.
- `impuestos`: opcional, default `0.00`.
- `total`: obligatorio y debe cumplir `subtotal - descuento + impuestos`.
- `referencia`: opcional.
- `observaciones`: opcional.

**Respuesta exitosa (201 Created)**

```json
{
  "factura": {
    "id": 48,
    "folio": "FAC-PEND-001",
    "estatus": "Emitida",
    "cliente": 15,
    "moneda": 1,
    "pedido": null,
    "subtotal": "1000.00",
    "descuento": "0.00",
    "impuestos": "160.00",
    "total": "1160.00",
    "fecha_vencimiento": "2026-07-31"
  },
  "cuenta_por_cobrar": {
    "id": 22,
    "estatus": "Pendiente",
    "saldo": "1160.00",
    "referencia": "PENDIENTE-JULIO",
    "fecha_vencimiento": "2026-07-31"
  },
  "poliza": {
    "id": 9,
    "folio": "POL-000009",
    "tipo": "Ingreso",
    "estatus": "Activo",
    "detalles": 3
  }
}
```

**Errores comunes (400)**

- `cliente`: cliente no encontrado o sin acceso.
- `moneda`: moneda no encontrada o sin acceso.
- `pedido`: pedido no encontrado, o no corresponde al cliente o moneda enviados.
- `folio`: ya existe una factura activa con ese folio.
- `total`: el total no coincide con `subtotal - descuento + impuestos`.
- `empresa`: el usuario no tiene empresa asignada.
- `sucursal`: el usuario no tiene una sucursal disponible para registrar la factura.
- `centro_costo`: no existe un centro de costo activo para generar la póliza.
- `cuenta_contable_cxc`: no existe una cuenta contable activa de tipo `Activo`.
- `cuenta_contable_ingreso`: no existe una cuenta contable activa de tipo `Ingreso`.
- `cuenta_contable_impuesto`: no existe una cuenta contable activa de tipo `Pasivo` cuando la factura incluye impuestos.

**Notas para Frontend (Next.js)**

- No es necesario enviar `empresa` ni `sucursal`; el backend las resuelve con el contexto del usuario autenticado.
- Si el frontend no quiere manejar folios manuales, puede enviar `folio` vacío y el backend lo genera.
- Este endpoint registra la factura con estatus `Emitida`, la cuenta por cobrar con estatus `Pendiente` y la póliza contable en la misma transacción.
- La póliza genera trazabilidad mínima para contabilidad: cargo a cuenta por cobrar, abono a ingreso neto y abono a impuestos cuando aplique.

### 2) Consultar cuentas por cobrar

- **Endpoint**: `GET /api/v1/finanzas/cuentas-por-cobrar/`
- **Detalle**: `GET /api/v1/finanzas/cuentas-por-cobrar/{id}/`

**Query Params (opcionales)**

- `cliente` o `cliente_id`: filtra por cliente.
- `estatus`: filtra por estatus exacto (`Pendiente`, `Parcial`, `Pagada`, `Cancelada`, `Vencida`).
- `saldo_pendiente=true`: devuelve solo registros con `saldo > 0`.
- `vencidas=true`: devuelve solo cuentas vencidas con saldo pendiente.

**Ejemplo**

`GET /api/v1/finanzas/cuentas-por-cobrar/?saldo_pendiente=true&vencidas=true`

**Respuesta de listado (200 OK)**

```json
[
  {
    "id": 22,
    "cliente": 15,
    "cliente_nombre": "Cliente Demo",
    "factura_id": 48,
    "factura_folio": "FAC-PEND-001",
    "moneda_id": 1,
    "moneda_codigo": "MXN",
    "fecha_emision": "2026-07-14",
    "fecha_vencimiento": "2026-07-31",
    "total": "1160.00",
    "saldo": "1160.00",
    "estatus": "Pendiente",
    "referencia": "PENDIENTE-JULIO",
    "fecha_ultimo_pago": null,
    "observaciones": "Factura pendiente por cobrar registrada manualmente",
    "created_at": "2026-07-14T18:10:00Z",
    "updated_at": "2026-07-14T18:10:00Z"
  }
]
```

**Respuesta de detalle (200 OK)**

```json
{
  "id": 22,
  "cliente": 15,
  "cliente_nombre": "Cliente Demo",
  "factura_id": 48,
  "factura_folio": "FAC-PEND-001",
  "moneda_id": 1,
  "moneda_codigo": "MXN",
  "fecha_emision": "2026-07-14",
  "fecha_vencimiento": "2026-07-31",
  "total": "1160.00",
  "saldo": "660.00",
  "total_pagado": "500.00",
  "estatus": "Parcial",
  "referencia": "PENDIENTE-JULIO",
  "fecha_ultimo_pago": "2026-07-20",
  "observaciones": "Factura pendiente por cobrar registrada manualmente",
  "factura": {
    "id": 48,
    "folio": "FAC-PEND-001",
    "estatus": "Emitida",
    "cliente": 15,
    "cliente_nombre": "Cliente Demo",
    "moneda": 1,
    "moneda_nombre": "MXN",
    "subtotal": "1000.00",
    "descuento": "0.00",
    "impuestos": "160.00",
    "total": "1160.00",
    "factura_detalles": []
  },
  "polizas": [
    {
      "id": 9,
      "folio": "POL-000009",
      "tipo": "Ingreso",
      "fecha": "2026-07-14",
      "concepto": "Factura por cobrar FAC-PEND-001 - Cliente Demo",
      "estatus": "Activo",
      "total_cargos": "1160.00",
      "total_abonos": "1160.00",
      "detalles": [
        {
          "id": 31,
          "cuenta_contable_id": 4,
          "cuenta_contable_codigo": "105-01",
          "cuenta_contable_nombre": "Clientes",
          "centro_costo_id": 1,
          "centro_costo_nombre": "General",
          "cargo": "1160.00",
          "abono": "0.00",
          "referencia": "PENDIENTE-JULIO",
          "observaciones": "Cargo por cuenta por cobrar de factura FAC-PEND-001.",
          "orden": 1
        }
      ]
    }
  ]
}
```

**Notas**

- El backend devuelve únicamente cuentas por cobrar ligadas a facturas activas de la empresa del usuario autenticado.
- Este endpoint es solo lectura; no crea ni modifica registros.
- El listado devuelve un resumen ligero.
- El detalle `GET /api/v1/finanzas/cuentas-por-cobrar/{id}/` devuelve la factura relacionada, el `total_pagado` y las pólizas asociadas a esa factura con sus partidas.

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

- **Endpoints CRUD**:
  - `GET /api/v1/produccion/orden-produccion/` — listado
  - `POST /api/v1/produccion/orden-produccion/` — alta
  - `GET /api/v1/produccion/orden-produccion/{id}/` — detalle
    - **Nuevo**: `pedido_vinculado: {id, folio}`. Nota: `OrdenProduccion.pedido` es `nullable`; si la OP no tiene pedido, `pedido_vinculado = null`.
- **Endpoints onboarding**:
  - `GET|POST /api/v1/produccion/orden-produccion/onboarding/`
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

### 7) Orden de Bordado Onboarding (patrón sencillo / manual)

- **Endpoints CRUD**:
  - `GET /api/v1/produccion/orden-bordado/` — listado (ligero, con cobertura)
  - `POST /api/v1/produccion/orden-bordado/` — alta
  - `GET /api/v1/produccion/orden-bordado/{id}/` — detalle
    - Entrega: encabezado OB + `detalles[]` completo (ubicaciones/configuración bordado), `cantidad_cubierta/contratada`, parcialidad por línea (`cantidad_pedido/asignada/pendiente`), `otras_ordenes_del_pedido[]`, `reparto_por_talla_aproximado`.
    - **Nuevo (último cambio)**: `pedido_vinculado: {id, folio}`, `avances[]` (historial de producción por operador), `resumen_avance` (totales + % avance), `maquina_asignada`.
    - **No incluye** lista de documentos ligados al pedido (cotización/OC/factura/...): la OB es documento de taller. Si los necesitas, usa `GET /api/v1/ventas/pedidos/{id}/`.
  - `PATCH /api/v1/produccion/orden-bordado/{id}/` — editar encabezado (estatus, máquina, prioridad, observaciones, reasignar operador).
  - `DELETE /api/v1/produccion/orden-bordado/{id}/` — soft delete (`activo=False`). Libera cupo del pedido para nuevas OBs.
- **Avances de producción (registrar bordado del día)**:
  - `GET /api/v1/produccion/bordado-avances/?ob=42` — listado filtrado por OB.
  - `POST /api/v1/produccion/bordado-avances/` — registrar producción. **En el body NO mandar `usuario`**: el backend usa SIEMPRE `request.user` (operador conectado). Body: `{ "ob": 42, "cantidad_bordada": 20, "puntadas_realizadas": 165000, "comentario": "Turno matutino" }`.
  - `DELETE /api/v1/produccion/bordado-avances/{id}/` — soft delete.
- **Onboarding**:
  - `GET /api/v1/produccion/orden-bordado/onboarding/`
  - `POST /api/v1/produccion/orden-bordado/onboarding/`

**GET onboarding — shape**

```json
{
  "pedidos": [
    {
      "id": 125,
      "folio": "PD-000125",
      "cliente": 15,
      "cliente_nombre": "Cliente Demo",
      "sucursal": 1,
      "sucursal_nombre": "Matriz",
      "detalles": [
        {
          "pedido_detalle_talla_id": 8821,
          "pedido_detalle_id": 3301,
          "producto_id": 77,
          "producto_nombre": "Gorra Legionario",
          "talla_id": 4,
          "talla_nombre": "CH",
          "color_id": 9,
          "color_nombre": "Azul Marino",
          "cantidad_pedido": 25.0,
          "cantidad_asignada": 10.0,
          "cantidad_pendiente": 15.0,
          "posicion_sugerida": "F",
          "ubicaciones": [
            {
              "codigo": "F",
              "ancho_cm": 10,
              "alto_cm": 5,
              "color_hilo": "ROJO"
            }
          ],
          "foto": { "url": "https://cdn.empresa.com/ref/bordado_gorra.jpg" },
          "notas": "Centrado en pecho"
        }
      ]
    }
  ],
  "operadores": [{ "id": 8, "nombre": "Juan Pérez" }],
  "preview": {
    "folio_ob_sugerido": "OB-20260730-001"
  }
}
```

**Reglas del GET onboarding**

| Campo                                      | Regla                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| ------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `pedidos`                                  | Solo pedidos con al menos una `PedidoDetalleTalla` con `lleva_bordado=True` **y con saldo pendiente**: si todas sus líneas están cubiertas al 100% por OBs activas, el pedido **no aparece** (no hay nada que bordar). Scope por `empresa` + `sucursales_permitidas()` del usuario.                                                                                                                                                                                                                                                                                                                                                                                                                           |
| `pedidos[].detalles[]`                     | Líneas del pedido que llevan servicio de bordado (una por cada combinación `producto + talla + color`) y con `cantidad > 0` —mismo criterio que aplica el POST, para no ofrecer renglones que luego rechaza—. Incluye `cantidad_pedido` y el preview de ubicaciones/foto/notas extraído desde `bordado_config`. Un pedido que sí aparece viaja con **todas** sus líneas, incluidas las ya agotadas (`cantidad_pendiente = 0`), para que el frontend pueda marcarlas.                                                                                                                                                                                                                                          |
| `cantidad_asignada` / `cantidad_pendiente` | Por línea. `cantidad_asignada` es la suma de `OrdenBordadoDetalle.cantidad` de **todas las OBs activas** de ese pedido para esa línea; `cantidad_pendiente = max(0, cantidad_pedido - cantidad_asignada)`. Es el valor con el que el frontend debe pre-llenar el selector de cantidades: usar `cantidad_pedido` ofrece piezas ya programadas y el POST las rechaza con 400. Los renglones de OB cuya `talla` quedó en `NULL` (los genera el pipeline de picking cuando la talla no trae `variante`) no se pueden atribuir a una talla concreta y se descuentan del pendiente de las líneas del mismo `pedido_detalle`, así que el **total por renglón** es exacto aunque el reparto por talla sea aproximado. |
| `operadores`                               | `Usuarios` activos de la empresa ordenados por nombre/email.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| `preview.folio_ob_sugerido`                | Usa SSoT `SerieFolio.preview_siguiente_folio()` (mismo modelo `nucleo.models.SerieFolio`). **Preview SIN consumo** (no gasta folio, no incrementa `folio_actual`).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| Sin empresa / sin sucursales asignadas     | Devuelve listas vacías `[]` sin error.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        |

**POST onboarding**

- **Mismo save que `create` tradicional** — usa el `OrdenBordadoSerializer` estándar.
- Body requerido mínimo: `{ "pedido": 125 }`.
- Opcionales: `prioridad`, `observaciones`.
- **Body opcional para selección de cantidades** (el ajuste nuevo): `detalles_override[]`. Si se envía, cada objeto del arreglo selecciona qué líneas y qué cantidades incluir en la OB.
- **Sin `detalles_override[]` (backwards compatible)**: comportamiento actual, se crean todas las líneas con 100% de su cantidad.
- **Con `detalles_override[]`**: solo se incluyen las líneas enviadas, con la cantidad solicitada. Nunca puede exceder `cantidad_pedido` (SSoT de `PedidoDetalleTalla.cantidad`).

Ejemplo body con selección parcial:

```json
{
  "pedido": 125,
  "prioridad": 2,
  "observaciones": "Primera mitad para taller A",
  "detalles_override": [
    { "pedido_detalle_talla_id": 8821, "cantidad": 15 },
    { "pedido_detalle_talla_id": 8822, "cantidad": 10 }
  ]
}
```

**Validaciones del serializer + service sobre `detalles_override[]` (y sin override)**

| Error                                                                                                                                        | HTTP Status                                                                                                                                                                         |
| -------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| IDs `pedido_detalle_talla_id` repetidos, inválidos, o que no pertenecen al mismo `pedido` que el body                                        | 400                                                                                                                                                                                 |
| `cantidad` no numérica, `<= 0`, fraccionaria (debe ser entero de piezas), o mayor a `PedidoDetalleTalla.cantidad` del renglón del pedido     | 400                                                                                                                                                                                 |
| Alguna línea enviada tiene `lleva_bordado=False`                                                                                             | 400                                                                                                                                                                                 |
| `detalles_override[]` vacío (no se seleccionó nada)                                                                                          | 400                                                                                                                                                                                 |
| Se intenta **crear sin override** una OB para un pedido que ya tiene una activa con el 100% de sus líneas                                    | 409 Conflict (mantiene la regla legacy SSoT, ver abajo)                                                                                                                             |
| **`ya_asignado + nuevo > disponible`** por alguna línea combinando todas las OBs activas (regla nueva de fraccionamiento seguro)             | 400                                                                                                                                                                                 |
| Se envía `detalles_override[]` seleccionando **solamente una parte** de las líneas y/o cantidades parciales **sin exceder el cupo restante** | Se permite (`201`). **No dispara 409**; se pueden crear múltiples OBs parciales activas hasta completar el pedido —la constraint que lo impedía se removió en la migración `0026`—. |

> **Regla de fraccionamiento SSoT** (`OrdenBordadoService._cantidades_asignadas_por_linea`): la suma de todos los `OrdenBordadoDetalle` activos para el mismo `(pedido_detalle_id, talla_id)` **no puede superar** `PedidoDetalleTalla.cantidad`. El error 400 del caso de exceso retorna además `detalles_exceso[]` con la línea exacta, lo pedido, lo ya asignado, lo nuevo solicitado y el cupo restante:```json
> {
> "err": "No se puede generar la orden de bordado...",
> "detalles_exceso": [

    "  - talla_id=4 pedido_detalle_id=3301: pedido=25.0, ya_asignado=15.0, solicitado=15.0, disponible_restante=10.0"

]
}

````

**SSoT de la configuración del bordado — SIN duplicación**

> Toda la configuración de bordado/ubicaciones/foto vive únicamente en `PedidoDetalleTalla.bordado_config` (modelo `ventas.PedidoDetalleTalla`, campos `lleva_bordado` + `bordado_config`). `OrdenBordadoDetalle` **no duplica** ese JSON; lo consulta en tiempo real por FK cruzada (`pedido_detalle_id` + `talla_id`), con caché por serializer para evitar N+1 en respuestas de `list`/`retrieve`.
>
> Lo que SÍ se guarda en `OrdenBordadoDetalle` (campos escalares de snapshot, legacy / compatibilidad):
> - `posicion_bordado` → derivado con fallback: `bordado_config.posicion` → `bordado_config.ubicaciones[0].codigo` → `bordado_config.ubicaciones[0].nombre`
> - `colores_hilo`, `puntadas` → si vienen en `bordado_config` o en la primera ubicación
> - `color` → tomado directamente de `PedidoDetalle.color` (FK del renglón de pedido)

**Respuesta 201 OK**

```json
{
  "id": 38,
  "pedido_folio": "PED-000001",
  "folio_bordado": "2026-OB-00001",
  "detalles": [
    {
      "id": 39,
      "producto_nombre": "Gorra Legionario",
      "talla_nombre": "CH",
      "color_nombre": "Azul Marino",
      "cantidad": 10.0,
      "posicion_bordado": "F",
      "colores_hilo": 2,
      "puntadas": 1500,
      "bordado_config": {
        "ubicaciones": [
          { "codigo": "F", "ancho_cm": 10, "alto_cm": 5, "color_hilo": "ROJO" }
        ],
        "foto": { "url": "https://cdn.empresa.com/ref/bordado_gorra.jpg" },
        "notas": "Centrado en pecho"
      },
      "ubicaciones": [
        { "codigo": "F", "ancho_cm": 10, "alto_cm": 5, "color_hilo": "ROJO" }
      ],
      "foto": { "url": "https://cdn.empresa.com/ref/bordado_gorra.jpg" },
      "notas": "Centrado en pecho"
    }
  ]
}
````

Campos calculados en `detalles[]` (todos read-only, derivados del SSoT `PedidoDetalleTalla.bordado_config`):

| Campo            | Fuente / Regla                                                                                                                                   |
| ---------------- | ------------------------------------------------------------------------------------------------------------------------------------------------ |
| `bordado_config` | JSON íntegro desde `PedidoDetalleTalla.bordado_config` (misma llave que usó Cotizaciones → Pedido). Retorna `null` si la talla no trae config.   |
| `ubicaciones[]`  | `bordado_config.ubicaciones` cuando es arreglo, de lo contrario `[]`.                                                                            |
| `foto`           | Busca la primera llave no vacía entre `foto / imagen / imagen_url / foto_url`. Si es string retorna `{ "url": string }`; si es dict lo deja así. |
| `notas`          | Busca la primera llave no vacía entre `notas / observaciones / comentarios`; `null` si ninguna.                                                  |

**Django Admin** — Producción

- `OrdenesBordado` (`OrdenesBordadoAdmin`): `list_display` id/folio/empresa/sucursal/pedido/estatus/usuario_asignado/prioridad, filtros y búsqueda por folio OB, folio de pedido, empresa, sucursal y operador. Incluye inline `OrdenBordadoDetalleInline` (renglones de la orden).
- `OrdenBordadoDetalle` (`OrdenBordadoDetalleAdmin`): listado standalone con filtros cruzados por `ob__empresa`, `ob__sucursal`, `estatus_bordado`, talla, color y `posicion_bordado`; búsqueda por folio OB, pedido, producto y posición.

**Ficha OB — Campos NUEVOS en `GET /api/v1/produccion/orden-bordado/{id}/`**

> Útiles para la ficha "hoja física de taller" de Next.js.
> ⚠️ **Nuevo enum estatus_bordado (NO se automatiza NINGÚN cambio — lo maneja el operador)**: 1 Sin trabajar (default), 2 Programado, 3 Ponchado, 4 Arreglo, 5 Bordando, 6 Detenido, 7 Finalizado. (Valor 8 = Cancelado legacy — no se muestra en UI).

```json
{
  "id": 42,
  "folio_bordado": "OB-20260818-0042",
  "estatus_bordado": 5,
  "estatus_bordado_display": "Bordando",
  "prioridad": 1,
  "maquina_asignada": "Barudan 1",
  "proveedor": 27,
  "proveedor_nombre": "Bordados Martínez S.A.",
  "proveedor_display": {
    "id": 27,
    "codigo": "PROV-BORD-027",
    "nombre": "Bordados Martínez S.A.",
    "razon_social": "Bordados Martínez S.A. de C.V.",
    "tipo": "Produccion",
    "rfc": "BMA890101ABC",
    "email": "produccion@bordadosmartinez.com",
    "telefono": "5512345678",
    "contacto_principal": "Ing. Pedro Martínez"
  },
  "observaciones": "Cliente pide logo nítido",
  "usuario_asignado": 5,
  "usuario_nombre": "Juan Pérez (supervisor)",
  "cantidad_cubierta": 60,
  "cantidad_contratada": 100,
  "cobertura_completa": false,

  "detalles": [
    {
      "id": 101,
      "pedido_detalle_id": 3301,
      "producto_nombre": "Playera",
      "talla_nombre": "M",
      "color_nombre": "Negro",
      "cantidad": 40,
      "puntadas": 8000,
      "posicion_bordado": "F"
    },
    {
      "id": 102,
      "pedido_detalle_id": 3301,
      "producto_nombre": "Playera",
      "talla_nombre": "L",
      "color_nombre": "Negro",
      "cantidad": 20,
      "puntadas": 8000,
      "posicion_bordado": "F"
    }
  ],

  "avances": [
    {
      "id": 1,
      "fecha": "2026-08-18T09:15:00Z",
      "usuario": 4,
      "usuario_nombre": "María Gómez",
      "orden_bordado_detalle": 101,
      "orden_bordado_detalle_display": {
        "id": 101,
        "producto_nombre": "Playera",
        "talla_nombre": "M",
        "color_nombre": "Negro",
        "cantidad_programada": 40,
        "posicion_bordado": "F"
      },
      "pedido_detalle_talla": 8821,
      "pedido_detalle_talla_display": {
        "id": 8821,
        "pedido_detalle_id": 3301,
        "talla_nombre": "M",
        "cantidad_pedido": 100
      },
      "cantidad_bordada": 25,
      "puntadas_por_pieza": 8000,
      "puntadas_realizadas": 200000,
      "puntadas_total": 200000,
      "comentario": "Turno matutino"
    },
    {
      "id": 2,
      "fecha": "2026-08-18T14:35:00Z",
      "usuario": 7,
      "usuario_nombre": "Luis Rodríguez",
      "orden_bordado_detalle": 101,
      "orden_bordado_detalle_display": {
        "id": 101,
        "producto_nombre": "Playera",
        "talla_nombre": "M",
        "color_nombre": "Negro",
        "cantidad_programada": 40,
        "posicion_bordado": "F"
      },
      "pedido_detalle_talla": 8821,
      "pedido_detalle_talla_display": {
        "id": 8821,
        "pedido_detalle_id": 3301,
        "talla_nombre": "M",
        "cantidad_pedido": 100
      },
      "cantidad_bordada": 15,
      "puntadas_por_pieza": 8000,
      "puntadas_realizadas": 120000,
      "puntadas_total": 120000,
      "comentario": "Terminé la talla M — vengo de la Playera talla CH"
    },
    {
      "id": 3,
      "fecha": "2026-08-18T15:10:00Z",
      "usuario": 4,
      "usuario_nombre": "María Gómez",
      "orden_bordado_detalle": 102,
      "orden_bordado_detalle_display": {
        "id": 102,
        "producto_nombre": "Playera",
        "talla_nombre": "L",
        "color_nombre": "Negro",
        "cantidad_programada": 20,
        "posicion_bordado": "F"
      },
      "pedido_detalle_talla": 8822,
      "pedido_detalle_talla_display": {
        "id": 8822,
        "pedido_detalle_id": 3301,
        "talla_nombre": "L",
        "cantidad_pedido": 50
      },
      "cantidad_bordada": 8,
      "puntadas_por_pieza": 8000,
      "puntadas_realizadas": 64000,
      "puntadas_total": 64000,
      "comentario": "Empiezo talla L"
    }
  ],

  "resumen_avance": {
    "cantidad_programada": 60,
    "cantidad_bordada_total": 48,
    "puntadas_presupuesto": 480000,
    "puntadas_por_pieza_promedio": 8000,
    "puntadas_realizadas": 384000,
    "puntadas_total": 384000,
    "porcentaje_avance": 80.0,
    "por_detalle": [
      {
        "orden_bordado_detalle_id": 101,
        "producto_nombre": "Playera",
        "talla_nombre": "M",
        "color_nombre": "Negro",
        "posicion_bordado": "F",
        "cantidad_programada": 40,
        "puntadas_presupuesto": 320000,
        "cantidad_bordada": 40,
        "puntadas_por_pieza_promedio": 8000,
        "puntadas_realizadas": 320000,
        "puntadas_total": 320000,
        "porcentaje_avance": 100.0,
        "operadores": [
          {
            "usuario_id": 4,
            "usuario_nombre": "María Gómez",
            "cantidad_bordada": 25,
            "puntadas_por_pieza_promedio": 8000,
            "puntadas_realizadas": 200000,
            "puntadas_total": 200000
          },
          {
            "usuario_id": 7,
            "usuario_nombre": "Luis Rodríguez",
            "cantidad_bordada": 15,
            "puntadas_por_pieza_promedio": 8000,
            "puntadas_realizadas": 120000,
            "puntadas_total": 120000
          }
        ]
      },
      {
        "orden_bordado_detalle_id": 102,
        "producto_nombre": "Playera",
        "talla_nombre": "L",
        "color_nombre": "Negro",
        "posicion_bordado": "F",
        "cantidad_programada": 20,
        "puntadas_presupuesto": 160000,
        "cantidad_bordada": 8,
        "puntadas_por_pieza_promedio": 8000,
        "puntadas_realizadas": 64000,
        "puntadas_total": 64000,
        "porcentaje_avance": 40.0,
        "operadores": [
          {
            "usuario_id": 4,
            "usuario_nombre": "María Gómez",
            "cantidad_bordada": 8,
            "puntadas_por_pieza_promedio": 8000,
            "puntadas_realizadas": 64000,
            "puntadas_total": 64000
          }
        ]
      }
    ]
  },

  "otras_ordenes_del_pedido": [],
  "pedido_vinculado": { "id": 125, "folio": "PD-000125" },
  "reparto_por_talla_aproximado": false
}
```

| Campo                                        | Tipo        | Fuente / Regla                                                                                                                                                                                                                     |
| -------------------------------------------- | ----------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `maquina_asignada`                           | str/null    | Campo editable. **Texto libre**: el operador escribe "M1", "Máquina 2", "Barudan 1", etc. (sin catálogo hoy).                                                                                                                      |
| `proveedor`                                  | int/null    | Campo editable. **FK al catálogo `terceros.Proveedor`** (usa el selector estándar de proveedores igual que Compras/Finanzas). NULL = bordado en planta interna, no subcontratado.                                                  |
| `proveedor_nombre`                           | str/null    | Read-only. Label corto del proveedor: `obj.proveedor.nombre`. NULL si `proveedor` es null.                                                                                                                                         |
| `proveedor_display`                          | object/null | Read-only. Label expandido para chip UI: `{id, codigo, nombre, razon_social, tipo, rfc, email, telefono, contacto_principal}`. NULL si no hay proveedor asignado.                                                                  |
| `avances[]`                                  | array       | Historial de producción por OB. Lo llena `POST /produccion/bordado-avances/`. Orden: `fecha` DESC.                                                                                                                                 |
| `avances[].usuario`                          | int         | FK al operador. **NO editable desde frontend**: backend siempre usa el `request.user` que envió el POST.                                                                                                                           |
| `avances[].orden_bordado_detalle`            | int/null    | FK al renglón concreto de la OB (la talla/SKU de esta tanda de producción). **Recomendado siempre mandarlo** desde el selector del frontend.                                                                                       |
| `avances[].orden_bordado_detalle_display`    | object      | Label listo para pintar en chip: producto, talla, color, cantidad programada, posición. NULL si el registro es viejo (antes del ajuste por talla).                                                                                 |
| `avances[].pedido_detalle_talla`             | int/null    | FK al `PedidoDetalleTalla`. **Backend lo autocompleta** si mandaste `orden_bordado_detalle_id` (no tienes que buscarlo tú en Ventas).                                                                                              |
| `avances[].puntadas_por_pieza`               | int         | **NUEVO**. Contador de puntadas POR PRENDA (1 pieza). 0 si no se capturó (registros legacy).                                                                                                                                       |
| `avances[].puntadas_realizadas`              | int         | Puntadas REALES de esta tanda (puedes meter el total de la tanda directamente o dejar que `puntadas_total` lo calcule). Distinto de `detalles[].puntadas` = presupuesto estimado por línea.                                        |
| `avances[].puntadas_total`                   | int         | **NUEVO y read-only calculado**. Resultado de `puntadas_por_pieza × cantidad_bordada`. Backend lo calcula automáticamente si `puntadas_por_pieza > 0`; si frontend envía valor, se sobreescribe con el cálculo (fuente de verdad). |
| `resumen_avance.por_detalle[]`               | array       | ⭐ **NUEVO**. Agrupación por cada renglón de la OB: cuánto va, cuánto presupuesto original, % y **quién trabajó ese SKU** (lista de operadores con su aporte). Sirve para pintar el grid "Avance por talla" sin más endpoints.     |
| `resumen_avance.por_detalle[].operadores[]`  | array       | Suma de `cantidad_bordada` + `puntadas_total` + `puntadas_por_pieza_promedio` agrupado por `usuario_id` para ESE renglón concreto. Ejemplo útil: "María 25pz + Luis 15pz = 40pz talla M = 100%".                                   |
| `resumen_avance.puntadas_por_pieza_promedio` | int         | **NUEVO**. Promedio PONDERADO de `puntadas_por_pieza` en toda la OB: `sum(por_pieza × cantidad) / sum(cantidad)`. 0 si ninguna tanda capturó puntadas por pieza.                                                                   |
| `resumen_avance.puntadas_total`              | int         | **NUEVO**. Suma de TODOS los `avances[].puntadas_total` de la OB. Equivale a `sum(puntadas_por_pieza × pz)` por tanda capturada.                                                                                                   |

**Editar encabezado de OB — `PATCH /api/v1/produccion/orden-bordado/{id}/`**

Body mínimo (solo los campos que quieras cambiar; los omitidos se dejan igual):

```json
{
  "estatus_bordado": 3,
  "maquina_asignada": "Barudan 1",
  "proveedor": 27,
  "prioridad": 2,
  "observaciones": "Urgente — cliente pide hoy",
  "usuario_asignado": 5
}
```

- `estatus_bordado` (**7 valores, NO se automatiza — 100% manual**):
  | # | Label
  | - | -----
  | 1 | Sin trabajar _(default al crear)_
  | 2 | Programado
  | 3 | Ponchado
  | 4 | Arreglo
  | 5 | Bordando
  | 6 | Detenido
  | 7 | Finalizado
  (8 = Cancelado legacy — no listar en UI, se mantiene en el enum por registros históricos).
- `proveedor`: **FK id a `/api/v1/terceros/proveedores/`**, nullable. NULL = planta interna. **Validación cross-tenant**: `proveedor.empresa_id` debe ser NULL (catálogo global) o coincidir con la empresa de la OB; si no → `400 {proveedor: ...}`.
- `usuario_asignado`: FK a usuario (jefe reasigna a alguien). Nulo permitido.
- Campos que SIGUEN read-only y se ignoran si los mandas: `folio_bordado`, `empresa`, `sucursal`, `activo`.

**Registrar producción — `POST /api/v1/produccion/bordado-avances/` (AHORA POR TALLA/SKU + PIEZAS × PUNTADAS)**

**Opción recomendada (frontend manda el renglón exacto de la OB + puntadas por pieza):**

```json
{
  "ob": 42,
  "orden_bordado_detalle": 101,
  "cantidad_bordada": 20,
  "puntadas_por_pieza": 8250,
  "puntadas_realizadas": 165000,
  "comentario": "Turno vespertino — sigo con la talla M"
}
```

> Con sólo mandar `orden_bordado_detalle` + `puntadas_por_pieza` el backend hace 4 cosas automáticas:
>
> - ✅ Valida que ese renglón pertenece a la `ob` (si no, 400 `orden_bordado_detalle` no pertenece).
> - ✅ Autocompleta `pedido_detalle_talla` cruzando `pedido_detalle_id + talla_id` del renglón (no tienes que buscarlo tú).
> - ✅ Asigna `usuario = request.user` (ignora el valor aunque lo mandaras).
> - ✅ Calcula **`puntadas_total = puntadas_por_pieza × cantidad_bordada`** = 8250 × 20 = 165,000. Si frontend envía `puntadas_total` manual, se sobreescribe con el cálculo.
>   _Sólo cuando `puntadas_por_pieza = 0` (registros legacy sin capturar) se deja pasar el valor que mande el cliente._

**Opción legacy (compatibilidad, registros sin detalle ni por_pieza):**

```json
{
  "ob": 42,
  "cantidad_bordada": 12,
  "puntadas_realizadas": 99000,
  "comentario": "Producción sin detalle específico"
}
```

- Si `orden_bordado_detalle = null`, en el resumen se agrupa en el renglón "Sin talla/SKU asignado (registro antiguo)" — la suma no se pierde pero no puede atribuirse a una talla.

**Reglas del avance por detalle-talla:**

- **Varios operadores = misma talla/SKU OK**. María POST 25pz talla M, luego Luis POST 15pz talla M: ambos usan el mismo `orden_bordado_detalle=101`; en `resumen_avance.por_detalle[].operadores` aparecen ambos agrupados y el renglón suma 100% cuando completan.
- `cantidad_bordada` no se valida contra la programada (acepta sobreproducción por errores/correcciones/rework).
- `puntadas_realizadas = 0` es permitido si aún no se lee el contador de la máquina.
- `puntadas_por_pieza = 0` se acepta (captura incompleta o históricos).
- `puntadas_total` siempre es fuente de verdad = producto × cantidad; no se modifica después del POST excepto por PATCH manual (supervisor).
- **Para corregir un registro mal capturado**: `DELETE /produccion/bordado-avances/{id}/` (soft delete — desaparece de `avances[]` y de sumatorias).
- **Campos write-once inmutables en update**: `orden_bordado_detalle` y `pedido_detalle_talla`. Si haces PATCH a un avance intentando reasignarlo a otra talla, el backend los descarta (no se puede mover producción de un SKU a otro retroactivamente).

**Control anti-duplicado (HTTP 409 Conflict)**

> Regla SSoT de negocio: no se permite crear más de una **OrdenBordado activa** para el mismo pedido si este ya cubre el 100% de las prendas con `lleva_bordado=True`. Evita doble consumo de folio OB y doble programación en taller.

- **Trigger**: segundo `POST /api/v1/produccion/orden-bordado/onboarding/` con el mismo `pedido` y la primera OB aún activa (no cancelada).
- **Status**: `409 Conflict`.
- **Payload de error extend**:

```json
{
  "err": "Ya existe una orden de bordado activa para este pedido con el 100% de las prendas. Si requiere dividir el bordado, contacte a producción.",
  "orden_bordado_existente": {
    "id": "38",
    "folio": "2026-OB-00001",
    "pedido": 125,
    "estado": "PENDIENTE",
    "url_detalle": "/api/v1/produccion/orden-bordado/38/"
  }
}
```

- **Garantía**: el consecutivo de `SerieFolio` para OrdenesBordado **no se consume** cuando responde 409. Antes del gasto transaccional de folio corre `OrdenBordadoService._validar_contexto` que incluye:
  - Validación **cross-tenant**: `pedido.empresa_id == user.empresa_id` y acceso por `sucursales_permitidas()`; si no, retorna 403/409 según caso y no gasta folio.
  - `buscar_existente_full_match()`: detecta OB activa para el mismo pedido con cobertura 100%.

**Estados y cancelación**: La constraint `uq_orden_bordado_activa_por_pedido` ("una OB activa por pedido") **ya no existe**: se removió en la migración `0026`, gemela de la `0025` que hizo lo propio con Reflejante y OCM, porque impedía la segunda OB parcial sobre el mismo pedido. Hoy un pedido puede tener **varias OBs activas** mientras la suma por línea no exceda `PedidoDetalleTalla.cantidad`.

Lo que controla el cupo ahora:

- **Por línea y por renglón**: `ya_asignado + nuevo <= cantidad_pedido`, contando sólo OBs con `activo=true`. Se valida en dos cortes —por `(pedido_detalle, talla)` y por total del `pedido_detalle`, este último para absorber los renglones con `talla = NULL` que genera el picking—. Excederlo es `400` con `detalles_exceso[]`.
- **409 de pedido completo**: sólo en el POST **sin** `detalles_override` y sólo si el pedido ya está cubierto al 100% **en piezas**. Una OB parcial que toca todas las líneas con cantidades reducidas ya no dispara este 409 (antes sí: la detección contaba renglones, no piezas).
- **Concurrencia**: `OrdenBordadoService.save()` toma un `select_for_update` sobre el renglón de `Pedido` antes de leer lo asignado, de modo que dos POST simultáneos sobre el mismo pedido se serializan en vez de pasar ambos el chequeo de cupo.
- **Cancelación**: cambiar el estatus a `CANCELADO` **no** libera cupo; sólo el soft delete (`activo=false`) lo devuelve, porque el cupo se calcula sobre OBs `activo=true`.

### 8) Orden de Reflejante Onboarding (patrón sencillo / manual)

- **Endpoints CRUD**:
  - `GET /api/v1/produccion/orden-reflejante/` — listado (ligero, con cobertura)
  - `POST /api/v1/produccion/orden-reflejante/` — alta
  - `GET /api/v1/produccion/orden-reflejante/{id}/` — detalle
    - **Nuevo**: `pedido_vinculado: {id, folio}`.
- **Endpoints onboarding**:
  - `GET /api/v1/produccion/orden-reflejante/onboarding/`
  - `POST /api/v1/produccion/orden-reflejante/onboarding/`
- **Objetivo**: patrón onboarding idéntico a ÓrdenesBordado y WMS: catálogos precargados para que Next.js muestre selector de pedido + operadores + preview folio.

> **Pendiente por línea**: cada objeto de `detalles[]` incluye `cantidad_asignada` y `cantidad_pendiente` (= `max(0, cantidad_pedido - cantidad_asignada)`) sobre las órdenes activas del pedido, y un pedido sin ninguna línea con saldo **no aparece** en `pedidos`. Misma semántica que el onboarding de Bordado; ver esa sección para el detalle de los renglones con `talla = NULL`.

**GET onboarding — shape**

```json
{
  "pedidos": [
    {
      "id": 125,
      "folio": "PD-000125",
      "cliente": 15,
      "cliente_nombre": "Cliente Demo",
      "sucursal": 1,
      "sucursal_nombre": "Matriz",
      "detalles": [
        {
          "pedido_detalle_talla_id": 8821,
          "pedido_detalle_id": 3301,
          "producto_id": 9,
          "producto_nombre": "Chamarra Industrial",
          "talla_id": 4,
          "talla_nombre": "M",
          "color_id": 7,
          "color_nombre": "Azul Marino",
          "cantidad_pedido": 25.0,
          "posicion_sugerida": "ESP",
          "ubicaciones": [
            {
              "codigo": "ESP",
              "ancho_cm": 30,
              "alto_cm": 4,
              "color_reflejante": "AMARILLO"
            }
          ],
          "foto": { "url": "https://.../ref_espalda.jpg" },
          "notas": "Banda reflejante completa en espalda"
        }
      ]
    }
  ],
  "operadores": [{ "id": 8, "nombre": "Juan Pérez" }],
  "preview": {
    "folio_or_sugerido": "OR-20260730-001"
  }
}
```

**Reglas del GET onboarding**

| Campo                                  | Regla                                                                                                                                                              |
| -------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `pedidos`                              | Solo pedidos con al menos una `PedidoDetalleTalla` con `lleva_reflejante=True`. Scope por `empresa` + `sucursales_permitidas()` del usuario.                       |
| `pedidos[].detalles`                   | Una fila por combinación `producto + talla + color` del pedido con `lleva_reflejante=True`. `pedido_detalle_talla_id` es el PK que usará el body de POST.          |
| `operadores`                           | `Usuarios` activos de la empresa ordenados por nombre/email.                                                                                                       |
| `preview.folio_or_sugerido`            | Usa SSoT `SerieFolio.preview_siguiente_folio()` (mismo modelo `nucleo.models.SerieFolio`). **Preview SIN consumo** (no gasta folio, no incrementa `folio_actual`). |
| Sin empresa / sin sucursales asignadas | Devuelve listas vacías `[]` sin error.                                                                                                                             |

**POST onboarding**

- **Mismo save que `create` tradicional** — usa el `OrdenReflejanteSerializer` estándar.
- Body requerido mínimo: `{ "pedido": 125 }`.
- Opcionales: `prioridad`, `observaciones`, `detalles_override[]`.
- **Sin `detalles_override`** (backwards compat): carga **todas** las `PedidoDetalleTalla` del pedido con `lleva_reflejante=True` al 100% de su cantidad, genera folio OR único y `bulk_create` de `OrdenReflejanteDetalle`.
- **Con `detalles_override[]`**: selector de líneas y cantidades para crear **OR parciales**. Cada renglón tiene `{ "pedido_detalle_talla_id": 8821, "cantidad": 10.0 }`. La suma de todas las ORs activas por línea está limitada por `PedidoDetalleTalla.cantidad` (SSoT).
- Al crear cada detalle se persisten como snapshot los campos escalares: `color`, `tipo_reflejante`, `posicion`, `metros_reflejante` (derivados de `reflejante_config`); **no** se duplica `reflejante_config` completo (vía FK `pedido_detalle` + `talla` la consulta al serializer directamente desde `PedidoDetalleTalla`).
- No depende de WMS ni de un picking existente; se genera completamente desde Producción.

Ejemplo de body con OR parcial:

```json
{
  "pedido": 125,
  "prioridad": 2,
  "observaciones": "Primera mitad para turno A",
  "detalles_override": [
    { "pedido_detalle_talla_id": 8821, "cantidad": 10.0 },
    { "pedido_detalle_talla_id": 8822, "cantidad": 8.0 }
  ]
}
```

**Validaciones del serializer + service sobre `detalles_override[]`**

| Error                                                                                                                                    | HTTP Status                                                                    |
| ---------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------ |
| IDs `pedido_detalle_talla_id` repetidos, inválidos, o que no pertenecen al mismo `pedido` que el body                                    | 400                                                                            |
| `cantidad` no numérica, `<= 0`, o mayor a `PedidoDetalleTalla.cantidad` del renglón del pedido                                           | 400                                                                            |
| Alguna línea enviada tiene `lleva_reflejante=False`                                                                                      | 400                                                                            |
| `detalles_override[]` vacío (no se seleccionó nada)                                                                                      | 400                                                                            |
| Se intenta **crear sin override** una OR para un pedido que ya tiene una activa con el 100% de sus líneas                                | 409 Conflict                                                                   |
| **`ya_asignado + nuevo > disponible`** por alguna línea combinando todas las ORs activas (regla de fraccionamiento seguro)               | 400                                                                            |
| Se envía `detalles_override[]` seleccionando solamente una parte de las líneas y/o cantidades parciales **sin exceder el cupo restante** | Se permite. Se pueden crear múltiples ORs parciales hasta completar el pedido. |

**Respuesta 201 OK**

```json
{
  "id": 22,
  "pedido_folio": "PED-000001",
  "folio_reflejante": "2026-OR-00001",
  "detalles": [
    {
      "id": 41,
      "producto_nombre": "Chamarra Industrial",
      "talla_nombre": "M",
      "color_nombre": "Azul Marino",
      "cantidad": 10.0,
      "tipo_reflejante": "Alto brillo",
      "posicion": "ESP",
      "metros_reflejante": 30,
      "reflejante_config": {
        "ubicaciones": [{ "codigo": "ESP", "ancho_cm": 30 }],
        "foto": { "url": "https://.../ref_espalda.jpg" },
        "notas": "Banda reflejante completa en espalda"
      },
      "ubicaciones": [{ "codigo": "ESP", "ancho_cm": 30 }],
      "foto": { "url": "https://.../ref_espalda.jpg" },
      "notas": "Banda reflejante completa en espalda"
    }
  ]
}
```

> **Fuente de verdad única (SSoT)**: la configuración de reflejante (`ubicaciones`, `foto`, `notas`) **vive solo en `ventas.PedidoDetalleTalla.reflejante_config`**. `OrdenReflejanteDetalle` no la duplica; el serializer la lee haciendo join por `(pedido_detalle_id, talla_id)` → PK única de `PedidoDetalleTalla`. Solo persiste como snapshot los campos escalares `tipo_reflejante` / `posicion` / `metros_reflejante` y `color` para los listados/operaciones del área de taller.

**Control anti-duplicado (HTTP 409 Conflict)**

> Regla SSoT de negocio: solo se dispara 409 cuando intentas **crear sin override** la cobertura 100% y ya existe una OR activa full-match. Cuando usas `detalles_override[]` para parcialidades, 409 no se activa; la protección contra repetir trabajo es el check `ya_asignado + nuevo <= disponible` (HTTP 400 con `detalles_exceso[]`).

- **Trigger**: segundo `POST /api/v1/produccion/orden-reflejante/onboarding/` con el mismo `pedido`, sin `detalles_override`, y la primera OR aún activa (no cancelada).
- **Status**: `409 Conflict`.
- **Payload de error extend**:

```json
{
  "err": "Ya existe una orden de reflejante activa para este pedido con el 100% de las prendas. Si requiere dividir el reflejante, contacte a producción.",
  "orden_reflejante_existente": {
    "id": "22",
    "folio": "2026-OR-00001",
    "pedido": 125,
    "estado": "PENDIENTE"
  }
}
```

Cuando la solicitud de OR parcial sí excede el cupo restante (validación de suma por línea), el error es **HTTP 400** con detalle desglosado:

```json
{
  "err": "No se puede generar la orden de reflejante: una o más líneas exceden la cantidad disponible del pedido.",
  "detalles_exceso": [
    "  - talla_id=4 pedido_detalle_id=3301: pedido=25.0, ya_asignado=15.0, solicitado=15.0, disponible_restante=10.0"
  ]
}
```

- **Garantía**: el consecutivo de `SerieFolio` para OrdenesReflejante **no se consume** cuando responde 409. Antes del gasto transaccional de folio corre `OrdenReflejanteService._validar_contexto` que incluye:
  - Validación **cross-tenant**: `pedido.empresa_id == user.empresa_id` y acceso por `sucursales_permitidas()`; si no, retorna error y no gasta folio.
  - `buscar_existente_full_match()`: detecta OR activa para el mismo pedido con cobertura 100%.
  - `_cantidades_asignadas_por_linea()` + check `ya_asignado + nuevo <= disponible` por cada `(pedido_detalle, talla)`.

**Estados y cancelación**: la protección de cupo se libera **sólo** al dar de baja la OR (soft delete, `activo=false`). Cambiar el estatus a `CANCELADO` **no** libera el pedido por sí solo: la OR sigue `activo=true`, así que sigue consumiendo cupo hasta que la OR previa se dé de baja. Se quitó la constraint `uq_orden_reflejante_activa_por_pedido` de Postgres para permitir múltiples ORs parciales por el mismo pedido; la guardia de consistencia ahora se valida en el service (suma por línea).

### 9) Orden de Corte de Manga Onboarding (patrón sencillo / manual)

- **Endpoints CRUD**:
  - `GET /api/v1/produccion/orden-corte-manga/` — listado (ligero)
  - `POST /api/v1/produccion/orden-corte-manga/` — alta
  - `GET /api/v1/produccion/orden-corte-manga/{id}/` — detalle
    - **Nuevo**: `pedido_vinculado: {id, folio}`.
- **Endpoints onboarding**:
  - `GET /api/v1/produccion/orden-corte-manga/onboarding/`
  - `POST /api/v1/produccion/orden-corte-manga/onboarding/`
- **Objetivo**: patrón onboarding idéntico a ÓrdenesBordado/ÓrdenesReflejante y WMS: catálogos precargados para que Next.js muestre selector de pedido + operadores + preview folio.

> **Pendiente por línea**: cada objeto de `detalles[]` incluye `cantidad_asignada` y `cantidad_pendiente` (= `max(0, cantidad_pedido - cantidad_asignada)`) sobre las órdenes activas del pedido, y un pedido sin ninguna línea con saldo **no aparece** en `pedidos`. Misma semántica que el onboarding de Bordado; ver esa sección para el detalle de los renglones con `talla = NULL`.

**GET onboarding — shape**

```json
{
  "pedidos": [
    {
      "id": 125,
      "folio": "PD-000125",
      "cliente": 15,
      "cliente_nombre": "Cliente Demo",
      "sucursal": 1,
      "sucursal_nombre": "Matriz",
      "detalles": [
        {
          "pedido_detalle_talla_id": 9005,
          "pedido_detalle_id": 3350,
          "producto_id": 11,
          "producto_nombre": "Camisa Térmica",
          "talla_id": 6,
          "talla_nombre": "L",
          "color_id": 3,
          "color_nombre": "Gris Oxford",
          "cantidad_pedido": 40.0,
          "posicion_sugerida": "CM",
          "ubicaciones": [
            {
              "codigo": "CM",
              "largo_cm": 15,
              "tipo_remate": "dobladillo_doble"
            }
          ],
          "foto": { "url": "https://.../corte_manga_ejemplo.jpg" },
          "notas": "Remate doble en dobladillo"
        }
      ]
    }
  ],
  "operadores": [{ "id": 8, "nombre": "Juan Pérez" }],
  "preview": {
    "folio_ocm_sugerido": "OCM-20260730-001"
  }
}
```

**Reglas del GET onboarding**

| Campo                                  | Regla                                                                                                                                                              |
| -------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `pedidos`                              | Solo pedidos con al menos una `PedidoDetalleTalla` con `lleva_corte_manga=True`. Scope por `empresa` + `sucursales_permitidas()` del usuario.                      |
| `pedidos[].detalles`                   | Una fila por combinación `producto + talla + color` del pedido con `lleva_corte_manga=True`. `pedido_detalle_talla_id` es el PK que usará el body de POST.         |
| `operadores`                           | `Usuarios` activos de la empresa ordenados por nombre/email.                                                                                                       |
| `preview.folio_ocm_sugerido`           | Usa SSoT `SerieFolio.preview_siguiente_folio()` (mismo modelo `nucleo.models.SerieFolio`). **Preview SIN consumo** (no gasta folio, no incrementa `folio_actual`). |
| Sin empresa / sin sucursales asignadas | Devuelve listas vacías `[]` sin error.                                                                                                                             |

**POST onboarding**

- **Mismo save que `create` tradicional** — usa el `OrdenesCorteMangaSerializer` estándar.
- Body requerido mínimo: `{ "pedido": 125 }`.
- Opcionales: `prioridad`, `observaciones`, `detalles_override[]`.
- **Sin `detalles_override`** (backwards compat): carga **todas** las `PedidoDetalleTalla` del pedido con `lleva_corte_manga=True` al 100% de su cantidad, genera folio OCM único y `bulk_create` de `OrdenCorteMangaDetalle`.
- **Con `detalles_override[]`**: selector de líneas y cantidades para crear **OCM parciales**. Cada renglón tiene `{ "pedido_detalle_talla_id": 9005, "cantidad": 20.0 }`. La suma de todas las OCMs activas por línea está limitada por `PedidoDetalleTalla.cantidad` (SSoT).
- Al crear cada detalle se persisten como snapshot: `color` y `configuracion` (JSON copiado de `corte_manga_config` de la PDT, por compatibilidad histórica del modelo); el serializer de detalle además lee `corte_manga_config` directamente desde `PedidoDetalleTalla` y hace merge con el `configuracion` del detalle para garantizar SSoT.
- No depende de WMS ni de un picking existente; se genera completamente desde Producción.

Ejemplo de body con OCM parcial:

```json
{
  "pedido": 125,
  "prioridad": 3,
  "observaciones": "Entrega parcial para tienda Matriz",
  "detalles_override": [{ "pedido_detalle_talla_id": 9005, "cantidad": 20.0 }]
}
```

**Validaciones del serializer + service sobre `detalles_override[]`**

| Error                                                                                                                                    | HTTP Status                                                                     |
| ---------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------- |
| IDs `pedido_detalle_talla_id` repetidos, inválidos, o que no pertenecen al mismo `pedido` que el body                                    | 400                                                                             |
| `cantidad` no numérica, `<= 0`, o mayor a `PedidoDetalleTalla.cantidad` del renglón del pedido                                           | 400                                                                             |
| Alguna línea enviada tiene `lleva_corte_manga=False`                                                                                     | 400                                                                             |
| `detalles_override[]` vacío (no se seleccionó nada)                                                                                      | 400                                                                             |
| Se intenta **crear sin override** una OCM para un pedido que ya tiene una activa con el 100% de sus líneas                               | 409 Conflict                                                                    |
| **`ya_asignado + nuevo > disponible`** por alguna línea combinando todas las OCMs activas (regla de fraccionamiento seguro)              | 400                                                                             |
| Se envía `detalles_override[]` seleccionando solamente una parte de las líneas y/o cantidades parciales **sin exceder el cupo restante** | Se permite. Se pueden crear múltiples OCMs parciales hasta completar el pedido. |

**Respuesta 201 OK**

```json
{
  "id": 14,
  "pedido_folio": "PED-000001",
  "folio_ocm": "2026-OCM-00001",
  "detalles": [
    {
      "id": 30,
      "producto_nombre": "Camisa Térmica",
      "talla_nombre": "L",
      "color_nombre": "Gris Oxford",
      "cantidad": 20.0,
      "corte_manga_config": {
        "ubicaciones": [{ "codigo": "CM", "largo_cm": 15 }],
        "foto": { "url": "https://.../corte_manga_ejemplo.jpg" },
        "notas": "Remate doble en dobladillo"
      },
      "ubicaciones": [{ "codigo": "CM", "largo_cm": 15 }],
      "foto": { "url": "https://.../corte_manga_ejemplo.jpg" },
      "notas": "Remate doble en dobladillo"
    }
  ]
}
```

> **Fuente de verdad única (SSoT)**: la configuración de corte de manga (`ubicaciones`, `foto`, `notas`) **vive solo en `ventas.PedidoDetalleTalla.corte_manga_config`**. El helper `get_corte_manga_config` del serializer hace merge con `OrdenCorteMangaDetalle.configuracion` (campo snapshot existente) para preservar compatibilidad con OCMs antiguas; los datos canonicales siguen siendo la PDT.

**Control anti-duplicado (HTTP 409 Conflict)**

> Regla SSoT de negocio: solo se dispara 409 cuando intentas **crear sin override** la cobertura 100% y ya existe una OCM activa full-match. Cuando usas `detalles_override[]` para parcialidades, 409 no se activa; la protección contra repetir trabajo es el check `ya_asignado + nuevo <= disponible` (HTTP 400 con `detalles_exceso[]`).

- **Trigger**: segundo `POST /api/v1/produccion/orden-corte-manga/onboarding/` con el mismo `pedido`, sin `detalles_override`, y la primera OCM aún activa (no cancelada).
- **Status**: `409 Conflict`.
- **Payload de error extend**:

```json
{
  "err": "Ya existe una orden de corte de manga activa para este pedido con el 100% de las prendas. Si requiere dividir el corte, contacte a producción.",
  "orden_corte_manga_existente": {
    "id": "14",
    "folio": "2026-OCM-00001",
    "pedido": 125,
    "estado": "PENDIENTE"
  }
}
```

Cuando la solicitud de OCM parcial sí excede el cupo restante (validación de suma por línea), el error es **HTTP 400** con detalle desglosado:

```json
{
  "err": "No se puede generar la orden de corte de manga: una o más líneas exceden la cantidad disponible del pedido.",
  "detalles_exceso": [
    "  - talla_id=6 pedido_detalle_id=3350: pedido=40.0, ya_asignado=25.0, solicitado=20.0, disponible_restante=15.0"
  ]
}
```

- **Garantía**: el consecutivo de `SerieFolio` para OrdenesCorteManga **no se consume** cuando responde 409. Antes del gasto transaccional de folio corre `OrdenCorteMangaService._validar_contexto` que incluye:
  - Validación **cross-tenant**: `pedido.empresa_id == user.empresa_id` y acceso por `sucursales_permitidas()`; si no, retorna error y no gasta folio.
  - `buscar_existente_full_match()`: detecta OCM activa para el mismo pedido con cobertura 100%.
  - `_cantidades_asignadas_por_linea()` + check `ya_asignado + nuevo <= disponible` por cada `(pedido_detalle, talla)`.

**Estados y cancelación**: la protección de cupo se libera **sólo** al dar de baja la OCM (soft delete, `activo=false`). Cambiar el estatus a `CANCELADO` **no** libera el pedido por sí solo: la OCM sigue `activo=true`, así que sigue consumiendo cupo hasta que la OCM previa se dé de baja. Se quitó la constraint `uq_orden_corte_manga_activa_por_pedido` de Postgres para permitir múltiples OCMs parciales por el mismo pedido; la guardia de consistencia ahora se valida en el service (suma por línea).

---

## 📦 WMS - Picking

### 1) Onboarding de Picking

- **Endpoint**: `GET /api/v1/wms/pickings/onboarding/`
- **Objetivo (rediseño v2: _Tracker de prendas por pedido_)**: dar al frontend todo lo necesario para preparar un surtido parcial o total mediante un flujo onboarding de 4 pasos. El **picking** es el documento que _rastrea la ruta física_ de las prendas del pedido:
  - `almacen_origen` → de dónde se toman las prendas.
  - `almacen_destino` → hacia qué estación/almacén se envían (seleccionable **libremente**).
  - Las cantidades `cantidad_asignada` / `cantidad_surtida` alimentan los dashboards de `% surtido del pedido` y `% avance de órdenes de trabajo` vinculadas al mismo pedido.
  - Este endpoint **no crea transferencias, no mueve inventario**.

**Flujo onboarding**

1. Seleccionar el **pedido** (catálogo `pedidos`).
2. Seleccionar **almacén origen** de la lista filtrada `almacenes_origen` (solo almacenes con `permite_salida=True`).
3. Seleccionar **almacén destino** de la lista filtrada `almacenes_destino` (solo almacenes con `permite_entrada=True`). Si no se envía query param, se sugiere `APARTADOS` como convención, pero **ya no es obligatorio**: el operador puede cambiarlo en el selector.
4. Vista previa del encabezado (`header` con fecha sugerida, folio preview y **`tracker` KPIs del pedido**) + líneas con **existencia disponible** para picking parcial y flags de órdenes de trabajo (bordado / reflejante / corte de manga).

**Query params**

| Param                                    | Requerido | Descripción                                                                             |
| ---------------------------------------- | --------- | --------------------------------------------------------------------------------------- |
| `pedido` / `pedido_id`                   | No        | Activa la precarga del pedido y sus líneas de talla.                                    |
| `almacen_origen` / `almacen_origen_id`   | No        | Preselecciona el almacén de origen; usado para calcular existencia disponible.          |
| `almacen_destino` / `almacen_destino_id` | No        | Preselecciona el almacén destino (si no se envía se sugiere `APARTADOS` no bloqueante). |

**Ejemplo**

- `GET /api/v1/wms/pickings/onboarding/?pedido_id=125&almacen_origen=3&almacen_destino=28`

**Respuesta resumida**

```json
{
  "pedidos": [
    {
      "id": 125,
      "folio": "PD-000125",
      "cliente": 15,
      "cliente_nombre": "Cliente Demo",
      "sucursal": 1,
      "sucursal_nombre": "Matriz"
    }
  ],
  "operadores": [{ "id": 8, "nombre": "Juan Perez" }],
  "almacenes": [
    {
      "id": 3,
      "codigo": "PT-MTY",
      "nombre": "Almacén PT Monterrey",
      "sucursal": 1,
      "tipo_almacen": "PT",
      "permite_entrada": true,
      "permite_salida": true,
      "permite_transferencia": true
    },
    {
      "id": 28,
      "codigo": "PROC-BORD",
      "nombre": "Estación de Bordado (PROCESO)",
      "sucursal": 1,
      "tipo_almacen": "PROCESO",
      "permite_entrada": true,
      "permite_salida": true,
      "permite_transferencia": true
    },
    {
      "id": 10,
      "codigo": "APTOS",
      "nombre": "APARTADOS",
      "sucursal": 1,
      "tipo_almacen": "PT",
      "permite_entrada": true,
      "permite_salida": false,
      "permite_transferencia": false
    }
  ],
  "almacenes_origen": [
    { "id": 3, "codigo": "PT-MTY", "nombre": "Almacén PT Monterrey", ... }
  ],
  "almacenes_destino": [
    { "id": 28, "codigo": "PROC-BORD", "nombre": "Estación de Bordado (PROCESO)", ... },
    { "id": 10, "codigo": "APTOS", "nombre": "APARTADOS", ... }
  ],
  "almacen_origen": { "id": 3, "codigo": "PT-MTY", "nombre": "Almacén PT Monterrey", "sucursal": 1 },
  "almacen_destino": { "id": 28, "codigo": "PROC-BORD", "nombre": "Estación de Bordado (PROCESO)", "sucursal": 1 },
  "header": {
    "fecha_picking_sugerida": "2026-08-20T09:15:00.123456Z",
    "folio_sugerido_preview": "PICK-000021",
    "tracker": {
      "pct_asignado_pedido": "30.0000",
      "pct_surtido_pedido":   "12.5000",
      "total_prendas_pedido": "80",
      "total_asignado":       "24",
      "total_surtido":        "10"
    }
  },
  "pedido": { ... },
  "picking_detalle": [ ... ]
}
```

**Nuevos campos — selectores de almacén (v2)**

| Campo               | Shape (cada item)                                                               | Cuándo usarlo en el UI                                                                                                                                                       |
| ------------------- | ------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `almacenes_origen`  | `{id, codigo, nombre, sucursal, tipo_almacen, permite_salida, permite_entrada}` | Úsalo como **datasource del selector ORIGEN**. Siempre `permite_salida=True` (garantizado).                                                                                  |
| `almacenes_destino` | `{id, codigo, nombre, sucursal, tipo_almacen, permite_salida, permite_entrada}` | Úsalo como **datasource del selector DESTINO**. Siempre `permite_entrada=True`. El usuario cambia libremente la sugerencia default de APARTADOS por cualquier otra estación. |
| `almacenes`         | Catálogo completo (sin filtros). Backward-compat.                               | Úsalo solo si el UI requiere vista completa del catálogo. Para los selectores de origen/destino prefiere los dos de arriba.                                                  |

> **Fallback seguro**: Si el catálogo de almacenes **no tiene bien prendidos los flags** `permite_salida` / `permite_entrada` (ej: data vieja sin configurar), ambos subsets `almacenes_origen` / `almacenes_destino` **degeneran al catálogo completo** para no bloquear al operador. El admin debe prender estos flags en el catálogo `Almacen` para obtener el filtro óptimo.

**`header.tracker` — KPIs del pedido (v2)**

Siempre presente (shape igual cuando no hay pedido seleccionado: zeros en strings de Decimal). Los porcentajes se calculan como `100 * valor / total_prendas_pedido`.

| Campo                  | Tipo  | Fuente / fórmula                                                             | UI sugerida                 |
| ---------------------- | ----- | ---------------------------------------------------------------------------- | --------------------------- |
| `total_prendas_pedido` | `str` | `SUM(PedidoDetalleTalla.cantidad)` de todo el pedido.                        | Card: "Prendas del pedido"  |
| `total_asignado`       | `str` | `SUM(PickingDetalle.cantidad_asignada)` en pickings activos (no cancelados). | Card: "Asignado a pickings" |
| `total_surtido`        | `str` | `SUM(PickingDetalle.cantidad_surtida)` en pickings activos.                  | Card: "Surtido"             |
| `pct_asignado_pedido`  | `str` | `100 * total_asignado / total_prendas_pedido` (4 decimales fijos).           | Progress bar: "% Asignado"  |
| `pct_surtido_pedido`   | `str` | `100 * total_surtido / total_prendas_pedido` (4 decimales fijos).            | Progress bar: "% Surtido"   |

**Notas sobre existencia y picking parcial**

- `existencia_fisica`: lo que físicamente hay en el almacén origen (suma de **todas las ubicaciones** del mismo producto/variante).
- `existencia_reservada`: lo que ya está bloqueado por **reservas `ACTIVA`** de **cualquier otro picking/documento** (misma empresa/sucursal, mismo almacén). No incluye reservas `APLICADA` porque una reserva aplicada ya habría sido consumida en su movimiento físico correspondiente. No se limita a las tallas del pedido actual: todos los pedidos que compiten por la misma clave de stock suman.
- `existencia_disponible` = `existencia_fisica` - `existencia_reservada`.
- `maximo_picking_permitido` = `min(cantidad_pendiente, existencia_disponible)`.
  - Ejemplo: si el pedido pide 50 pz y solo hay 30 disponibles, el máximo que podrá enviarse en `cantidad_asignada` es 30 (picking parcial).
- **Validación agregada por clave de stock**: cuando varias líneas/tallas del pedido tienen `variante = null` y comparten el mismo `producto`, todas consumen la misma clave `(producto_id, None)`. El backend suma todas las `cantidad_asignada` de esa clave y valida el conjunto contra la existencia agregada (no solo línea por línea). El frontend puede asumir que cada línea individual cumple, pero el total de líneas con la misma clave no excederá el stock real.

**Notas sobre `header.folio_sugerido_preview`**

- El preview usa la misma lógica de formato que `SerieFolio.get_siguiente_folio()` (incluye `serie`, `relleno_ceros`, `separador`, `incluir_anio` y reinicios anuales), por lo que coincide con el folio real asignado en el POST.
- **No es vinculante**: es solo una sugerencia pre-visualización. Dos usuarios que hagan el GET onboarding simultáneamente verán el mismo preview; el consecutivo real y definitivo se reserva de forma transaccional dentro del `POST` de creación del picking.

**Indicadores de órdenes de trabajo**

Por cada línea/talla se exponen tres booleanos + su JSON config:

| Campo                  | Significado                           | UI sugerida                             |
| ---------------------- | ------------------------------------- | --------------------------------------- |
| `requiere_bordado`     | La prenda del pedido incluye bordado. | Mostrar indicador visual (badge/ícono). |
| `requiere_reflejante`  | La prenda incluye reflejante.         | Mostrar indicador visual.               |
| `requiere_corte_manga` | La prenda requiere corte de manga.    | Mostrar indicador visual.               |

> Las órdenes de trabajo **no se generan automáticamente** en el POST de creación del picking. Si la prenda requiere bordado/reflejante/corte, se genera su orden correspondiente usando **los endpoints del módulo Producción** (ej: `POST /api/v1/produccion/orden-bordado/onboarding/`).

---

### 2) Crear Picking desde Onboarding

- **Endpoint**: `POST /api/v1/wms/pickings/onboarding/` (o `POST /api/v1/wms/pickings/`)
- **Objetivo (rediseño v2: _Tracker de prendas_)**: **solo crear el documento** `Picking` + `PickingDetalle` con su folio único. Este paso **no mueve inventario, no crea transferencias, no crea reservas ni órdenes de producción**. El documento registra la intención de ruta: `almacén origen → almacén destino` + cantidades asignadas.

**Qué hace el POST (3 pasos)**

1. Next.js envía encabezado + `picking_detalle` con las cantidades reales a surtir por línea/talla, y el `almacen_destino` seleccionado por el operador (libre, desde `almacenes_destino`).
2. El backend valida (**errores campo-específicos `{"almacen_destino": "..."}` para poder bindear al form en Next.js**):
   - `cantidad_asignada` ≤ `cantidad_pendiente` de la talla.
   - `cantidad_asignada` ≤ `existencia_disponible` en el almacén origen (comparación contra existencia agregada por clave de stock).
   - **Validación agregada por clave**: la suma de `cantidad_asignada` de todas las líneas que comparten la misma clave `(producto_id, variante_id)` (incluyendo `variante_id = null`) debe ser ≤ la existencia disponible agregada de esa clave. Previene que múltiples tallas de un mismo producto sin variante agoten colectivamente el stock.
   - **Validación de almacenes (nuevas en v2)**:
     - `almacen.permite_salida = True` → 400 `{"almacen": "..."}` si no cumple.
     - `almacen_destino.permite_entrada = True` → 400 `{"almacen_destino": "..."}` si no cumple.
     - `almacen != almacen_destino` → 400 `{"almacen", "almacen_destino"}` (ambos campos) si son el mismo.
     - Cross-tenant: ambos almacenes deben pertenecer a la misma `empresa` y `sucursal` permitidas del usuario.
3. Crea el documento `Picking` + `PickingDetalle` (bulk_create), genera folio único y responde el picking.

> La **operación física** (tomar prendas del almacén origen y depositarlas en el destino) queda **fuera de este endpoint**. Para el movimiento de inventario se usan los endpoints del módulo Transferencias; para órdenes de producción se usa el módulo Producción.
>
> Alimentación de dashboards: en cuanto el picking se crea, el `header.tracker` del GET onboarding aumenta `total_asignado` y `pct_asignado_pedido` según las cantidades asignadas en ese documento. Cuando más adelante se marcan líneas como `SURTIDA` (vía actualizaciones de `PickingDetalle`), aumenta `total_surtido` y `pct_surtido_pedido`.

**Body**

```json
{
  "pedido": 125,
  "operador": 8,
  "almacen": 3,
  "almacen_destino": 28,
  "prioridad": "MEDIA",
  "tipo": "ORDER_PICKING",
  "observaciones": "Surtido parcial 30 pz, envío directo a estación de bordado (alm. 28)",
  "picking_detalle": [
    {
      "pedido_detalle_talla": 990,
      "cantidad_asignada": "30.0000",
      "observaciones": "Bordado especial — ya abierto OB en Produccion"
    },
    {
      "pedido_detalle_talla": 991,
      "cantidad_asignada": "2.0000"
    }
  ]
}
```

**Campos requeridos**

- `pedido`
- `operador`
- `almacen` (almacén origen)
- `picking_detalle` (cada línea con `pedido_detalle_talla` y `cantidad_asignada > 0`)
- **`almacen_destino`**: en la práctica **el UI debe enviarlo** (proviene del selector `almacenes_destino`). Si no se envía, el backend intenta una sugerencia por conveniencia (ver abajo).

**Campos opcionales / low-noise**

- `almacen_destino`: si se **omite** del body, el backend lo resuelve en este orden:
  1. Busca el almacén `APARTADOS` (nombre iexact) de la misma empresa + sucursal del pedido.
  2. Si `APARTADOS` no existe, devuelve **HTTP 400** `{"almacen_destino": "Selecciona un almacén destino. No existe un APARTADOS default configurado para Sucursal X."}` — pide al usuario que elija uno del selector.
- `prioridad`: `BAJA`, `MEDIA`, `ALTA`
- `tipo`: `ORDER_PICKING`, `BATCH_PICKING`, `WAVE_PICKING`, `ZONE_PICKING`
- `oleada`, `zona_almacen`, `lote`
- `fecha_inicio`, `fecha_fin`, `fecha_limite`
- `observaciones`
- Por cada línea en `picking_detalle`:
  - `generar_orden_bordado` / `generar_orden_reflejante` / `generar_orden_corte_manga`: **aceptados pero IGNORADOS en el v2 de create**. Las órdenes de trabajo se generan desde Produccion endpoints dedicados. No se rechazan para mantener bajo ruido con Next.js.
  - `observaciones`

**Validaciones principales (resumen)**

- El pedido debe pertenecer a la empresa del usuario.
- El almacén origen y destino deben pertenecer a la misma empresa y sucursal del pedido.
- El almacén origen **debe tener** `permite_salida=True`.
- El almacén destino **debe tener** `permite_entrada=True`.
- El almacén origen y destino **no** pueden ser el mismo (400 campo-específico en `almacen` + `almacen_destino`).
- El operador debe estar activo y pertenecer a la misma empresa.
- Cada renglón debe incluir `pedido_detalle_talla` y `cantidad_asignada > 0`.
- Cada cantidad enviada **no** puede exceder lo pendiente del pedido para esa talla.
- Cada cantidad enviada **no** puede exceder la `existencia_disponible` en el almacén origen.
- El avance del surtido no se guarda en `Pedido`; se calcula desde `PickingDetalle` (lo expone `header.tracker` en el GET onboarding).

**Respuesta**

```json
{
  "id": 14,
  "folio": "PICK-000014",
  "pedido": 125,
  "pedido_folio": "PD-000125",
  "operador": 8,
  "operador_nombre": "Juan Perez",
  "almacen": 3,
  "almacen_nombre": "Almacén PT Monterrey",
  "almacen_destino": 28,
  "almacen_destino_nombre": "Estación de Bordado (PROCESO)",
  "prioridad": "MEDIA",
  "tipo": "ORDER_PICKING",
  "estado": "Pendiente",
  "total_lineas": 2,
  "total_lineas_completas": 0,
  "observaciones": "Surtido parcial 30 pz",
  "ordenes_trabajo_generadas": [],
  "picking_detalle": [
    {
      "id": 51,
      "pedido_detalle": 301,
      "pedido_detalle_talla": 990,
      "producto": 22,
      "producto_nombre": "Playera Dry Fit",
      "producto_variante": 91,
      "producto_variante_nombre": "Playera Dry Fit Negra M",
      "talla_id": 4,
      "talla_nombre": "M",
      "cantidad_solicitada": "30.0000",
      "cantidad_asignada": "30.0000",
      "cantidad_surtida": "0.0000",
      "estado": "PENDIENTE",
      "operador": 8,
      "operador_nombre": "Juan Perez",
      "ubicacion": null,
      "ubicacion_nombre": null,
      "lote": null,
      "fecha_surtido": null,
      "diferencia": "0.0000",
      "motivo_diferencia": null,
      "observaciones": "Bordado especial"
    }
  ]
}
```

**Notas para Next.js (v2 rediseño)**

- Sí es necesario enviar `picking_detalle` en el onboarding `POST`.
- El frontend decide qué tallas y cantidades se surtirán en ese picking; debe **respetar** `maximo_picking_permitido` reportado por el GET onboarding (el backend lo validará de nuevo).
- **Usa los selectores nuevos**: `almacenes_origen` para el selector de origen, `almacenes_destino` para el de destino. El usuario puede elegir **cualquier almacén válido** como destino, no solo `APARTADOS`.
- Maneja el 400 **campo-específico**: la mayoría de errores de almacén vienen como `{"almacen_destino": "..."}` o `{"almacen": "..."}` — muestralos sobre el selector correcto.
- Los checkbox `generar_orden_*` pueden mandarse pero se ignoran en el POST de picking; la generación de OT se hace desde endpoints Produccion (ver sección `Orden de Bordado Onboarding`).
- El `Pedido` es solo referencia comercial; el avance real se consulta desde:
  - `GET onboarding` **`header.tracker`** (KPI compacto para encabezado del form).
  - Historial de `PickingDetalle` si se requiere vista granular.
- Si frontend necesita mostrar el surtido creado, puede usar la respuesta del `POST` o consultar el `GET` de detalle.
- `ordenes_trabajo_generadas` siempre devuelve `[]` en este endpoint. Las OT se vinculan desde sus endpoints de Producción.

### 3) Listar Pickings

- **Endpoint**: `GET /api/v1/wms/pickings/`
- **Descripción**: devuelve los pickings visibles para la empresa y sucursales del usuario autenticado.
- **Nota**: cada registro incluye `almacen_destino`, `almacen_destino_nombre` y el nested `ordenes_trabajo[]` (vacío para pickings creados con el flujo tradicional; con valores cuando OT se vinculan manualmente por PickingOrdenTrabajo desde Producción).

### 4) Detalle de Picking

- **Endpoint**: `GET /api/v1/wms/pickings/{id}/`
- **Descripción**: devuelve encabezado y `picking_detalle` del surtido.
- **Nota**: incluye `almacen_destino`, `almacen_destino_nombre` y expone el nested **`ordenes_trabajo[]`** (read-only, `related_name="ordenes_trabajo"` del modelo `PickingOrdenTrabajo`). Cada renglón contiene:
  - `tipo_orden`: `BORDADO` | `REFLEJANTE` | `CORTE_MANGA` (enum).
  - `tipo_orden_label`: label humano del tipo (ej: `"Bordado"`).
  - `orden_bordado` / `orden_reflejante` / `orden_corte_manga`: FK id al módulo de producción correspondiente (dos serán `null`, uno tendrá valor).
  - `orden_bordado_folio` / `orden_reflejante_folio` / `orden_corte_manga_folio`: folio formateado de la OT asociada (o `null`).

---

## 📦 WMS - Packing

### 1) Onboarding de Packing

- **Endpoint**: `GET /api/v1/wms/packings/onboarding/`
- **Objetivo**: dar a frontend los `pickings` disponibles para empacar y, al seleccionar uno, devolver exactamente qué líneas siguen pendientes.

**Flujo**

- Si se consulta sin `picking`, regresa el catálogo base de `pickings` visibles para el usuario.
- Si se envía `picking` o `picking_id`, además regresa:
  - encabezado del `picking`
  - líneas `packing_detalle` candidatas
  - cantidad ya empacada históricamente
  - cantidad pendiente por empacar por cada `picking_detalle`

**Ejemplo**

- `GET /api/v1/wms/packings/onboarding/?picking_id=14`

**Respuesta resumida**

```json
{
  "pickings": [
    {
      "id": 14,
      "folio": "PICK-000014",
      "pedido": 125,
      "pedido_folio": "PD-000125",
      "cliente_nombre": "Cliente Demo",
      "sucursal": 1,
      "sucursal_nombre": "Matriz",
      "operador": 8,
      "operador_nombre": "Juan Perez",
      "almacen": 3,
      "almacen_nombre": "Almacén PT Monterrey",
      "estado": "Pendiente"
    }
  ],
  "picking": {
    "id": 14,
    "folio": "PICK-000014",
    "pedido": 125,
    "pedido_folio": "PD-000125",
    "cliente": 15,
    "cliente_nombre": "Cliente Demo",
    "sucursal": 1,
    "sucursal_nombre": "Matriz",
    "operador": 8,
    "operador_nombre": "Juan Perez",
    "almacen": 3,
    "almacen_nombre": "Almacén PT Monterrey",
    "estado": "Pendiente"
  },
  "packing_detalle": [
    {
      "picking_detalle": 51,
      "pedido_detalle": 301,
      "pedido_detalle_talla": 990,
      "producto": 22,
      "producto_nombre": "Playera Dry Fit",
      "producto_variante": 91,
      "producto_variante_nombre": "Playera Dry Fit Negra M",
      "talla": 4,
      "talla_nombre": "M",
      "color": 2,
      "color_nombre": "Negro",
      "ubicacion": null,
      "ubicacion_nombre": null,
      "cantidad_solicitada": "4.0000",
      "cantidad_asignada": "4.0000",
      "cantidad_surtida": "0.0000",
      "cantidad_ya_empacada": "1.0000",
      "cantidad_pendiente_empacar": "3.0000",
      "estado": "PENDIENTE"
    }
  ]
}
```

### 2) Crear Packing desde Onboarding

- **Endpoint**: `POST /api/v1/wms/packings/onboarding/`
- **Objetivo**: registrar el empaque real de una o más líneas de un `picking`, validando contra el pendiente histórico para evitar sobre-empaque.

**Flujo actual**

- Next.js consulta primero el onboarding `GET`.
- El usuario selecciona un `picking` y decide cuánto empacar por cada `picking_detalle`.
- El backend valida:
  - empresa y sucursal del `picking`
  - acceso del usuario a la sucursal
  - que cada `picking_detalle` pertenezca al `picking`
  - que la suma enviada no exceda la cantidad pendiente por empacar
- Si todo es válido, el backend genera el `packing` con su `folio` y crea sus renglones `packing_detalle`.

**Body**

```json
{
  "picking": 14,
  "numero_cajas": 2,
  "peso_total": "12.500",
  "volumen_total": "0.850",
  "observaciones": "Empaque parcial para salida urgente",
  "packing_detalle": [
    {
      "picking_detalle": 51,
      "cantidad_empacada": "3.0000"
    },
    {
      "picking_detalle": 52,
      "cantidad_empacada": "2.0000",
      "observaciones": "Caja separada"
    }
  ]
}
```

**Campos requeridos**

- `picking`
- `packing_detalle`

**Campos opcionales**

- `numero_cajas`
- `peso_total`
- `volumen_total`
- `fecha_inicio`
- `fecha_fin`
- `observaciones`

**Validaciones principales**

- El `picking` debe pertenecer a la empresa del usuario.
- El usuario debe tener acceso a la sucursal del `picking`.
- No se puede empacar un `picking` cancelado.
- Cada línea debe incluir `picking_detalle` y `cantidad_empacada > 0`.
- Cada `picking_detalle` debe pertenecer al `picking` seleccionado.
- La suma enviada por línea no puede exceder la cantidad pendiente histórica.
- El backend contabiliza como histórico todos los `packing_detalle` no cancelados del mismo `picking`.

**Respuesta**

```json
{
  "id": 7,
  "folio": "PAC-000007",
  "pedido": 125,
  "pedido_folio": "PD-000125",
  "picking": 14,
  "picking_folio": "PICK-000014",
  "picking_estado": "Pendiente",
  "picking_almacen": 3,
  "picking_almacen_nombre": "Almacén PT Monterrey",
  "operador": 8,
  "operador_nombre": "Juan Perez",
  "usuario": 3,
  "usuario_nombre": "Desarrollo",
  "numero_cajas": 2,
  "peso_total": "12.500",
  "volumen_total": "0.850",
  "observaciones": "Empaque parcial para salida urgente",
  "packing_detalle": [
    {
      "id": 21,
      "packing": 7,
      "picking_detalle": 51,
      "pedido_detalle": 301,
      "pedido_detalle_talla": 990,
      "producto": 22,
      "producto_nombre": "Playera Dry Fit",
      "producto_variante": 91,
      "producto_variante_nombre": "Playera Dry Fit Negra M",
      "talla_id": 4,
      "talla_nombre": "M",
      "cantidad_solicitada": "4.0000",
      "cantidad_asignada": "4.0000",
      "cantidad_surtida": "0.0000",
      "cantidad_empacada": "3.0000",
      "ubicacion": null,
      "ubicacion_nombre": null,
      "caja": null,
      "caja_numero": null,
      "estado": "PENDIENTE",
      "observaciones": null
    }
  ]
}
```

**Notas para Next.js**

- El flujo recomendado es `GET /packings/onboarding/` -> seleccionar `picking` -> `POST /packings/onboarding/`.
- `POST /api/v1/wms/packings/` sigue funcionando, pero para onboarding conviene usar la misma URL `/onboarding/` para mantener el mismo patrón mental que `picking`.
- El frontend no necesita recalcular cantidades históricas; el backend devuelve `cantidad_ya_empacada` y `cantidad_pendiente_empacar`.
- Si un usuario tiene acceso a varias sucursales, el backend valida contra sus sucursales permitidas, no solo contra `sucursal_default`.

### 3) Listar Packings

- **Endpoint**: `GET /api/v1/wms/packings/`
- **Descripción**: devuelve los packings visibles para la empresa y sucursales del usuario autenticado.

### 4) Detalle de Packing

- **Endpoint**: `GET /api/v1/wms/packings/{id}/`
- **Descripción**: devuelve encabezado y `packing_detalle` del empaque registrado.

---

## 📦 WMS - Despacho

### 1) Onboarding de Despacho

- **Endpoint**: `GET /api/v1/wms/despachos/onboarding/`
- **Objetivo**: dar a frontend los `packings` disponibles para despacho y, al seleccionar uno, devolver los `envios` del mismo pedido y las líneas `packing_detalle` que siguen pendientes por despachar.

**Ligado al modelo**

- `despacho` va ligado a `packing` y opcionalmente a `envio`
- `despacho_detalle` va ligado a `despacho` y `packing_detalle`
- esto sigue exactamente la estructura de `doc/dbdiagram.io.md`

**Flujo**

- Si se consulta sin `packing`, regresa el catálogo base de `packings` visibles para el usuario.
- Si se envía `packing` o `packing_id`, además regresa:
  - encabezado del `packing`
  - `envios` del mismo pedido
  - líneas `despacho_detalle` candidatas
  - bandera `ya_despachado`
  - bandera `disponible_para_despacho`

**Ejemplo**

- `GET /api/v1/wms/despachos/onboarding/?packing_id=7`

**Respuesta resumida**

```json
{
  "packings": [
    {
      "id": 7,
      "folio": "PAC-000007",
      "pedido": 125,
      "pedido_folio": "PD-000125",
      "cliente_nombre": "Cliente Demo",
      "sucursal": 1,
      "sucursal_nombre": "Matriz",
      "picking": 14,
      "picking_folio": "PICK-000014",
      "almacen": 3,
      "almacen_nombre": "Almacén PT Monterrey",
      "estado": "PENDIENTE"
    }
  ],
  "envios": [
    {
      "id": 5,
      "pedido": 125,
      "transportista": 2,
      "transportista_nombre": "Transportes Demo"
    }
  ],
  "packing": {
    "id": 7,
    "folio": "PAC-000007",
    "pedido": 125,
    "pedido_folio": "PD-000125",
    "cliente": 15,
    "cliente_nombre": "Cliente Demo",
    "sucursal": 1,
    "sucursal_nombre": "Matriz",
    "picking": 14,
    "picking_folio": "PICK-000014",
    "almacen": 3,
    "almacen_nombre": "Almacén PT Monterrey",
    "estado": "PENDIENTE"
  },
  "despacho_detalle": [
    {
      "packing_detalle": 21,
      "picking_detalle": 51,
      "pedido_detalle": 301,
      "pedido_detalle_talla": 990,
      "producto": 22,
      "producto_nombre": "Playera Dry Fit",
      "producto_variante": 91,
      "producto_variante_nombre": "Playera Dry Fit Negra M",
      "talla": 4,
      "talla_nombre": "M",
      "color": 2,
      "color_nombre": "Negro",
      "ubicacion": null,
      "ubicacion_nombre": null,
      "caja": null,
      "caja_numero": null,
      "cantidad_empacada": "3.0000",
      "estado": "PENDIENTE",
      "ya_despachado": false,
      "disponible_para_despacho": true
    }
  ]
}
```

### 2) Crear Despacho desde Onboarding

- **Endpoint**: `POST /api/v1/wms/despachos/onboarding/`
- **Objetivo**: registrar qué líneas empacadas salen hacia logística, con o sin `envio` asociado en ese momento.

**Flujo actual**

- Next.js consulta primero el onboarding `GET`.
- El usuario selecciona un `packing`.
- El frontend obtiene los `envios` del mismo pedido y las líneas `packing_detalle` disponibles.
- El backend valida:
  - empresa y sucursal del `packing`
  - acceso del usuario a la sucursal
  - si `envio` viene informado, que pertenezca al mismo pedido y sucursal del `packing`
  - que cada `packing_detalle` pertenezca al `packing`
  - que la línea no haya sido despachada antes
- Si todo es válido, el backend crea `despacho` y sus renglones `despacho_detalle`.

**Body con envío**

```json
{
  "packing": 7,
  "envio": 5,
  "despacho_detalle": [
    {
      "packing_detalle": 21
    },
    {
      "packing_detalle": 22
    }
  ]
}
```

**Body sin envío**

```json
{
  "packing": 7,
  "despacho_detalle": [
    {
      "packing_detalle": 21
    },
    {
      "packing_detalle": 22
    }
  ]
}
```

**Campos requeridos**

- `packing`
- `despacho_detalle`

**Campo opcional**

- `envio`

**Validaciones principales**

- El `packing` debe pertenecer a la empresa del usuario.
- Si se envía `envio`, debe pertenecer a la misma empresa del usuario.
- Si se envía `envio`, debe corresponder al mismo `pedido` y `sucursal` del `packing`.
- No se puede despachar un `packing` cancelado.
- Cada línea debe incluir `packing_detalle`.
- Cada `packing_detalle` debe pertenecer al `packing` seleccionado.
- No se puede volver a despachar una línea ya registrada en otro `despacho`.

**Respuesta**

```json
{
  "id": 3,
  "packing": 7,
  "packing_folio": "PAC-000007",
  "packing_estado": "PENDIENTE",
  "pedido": 125,
  "pedido_folio": "PD-000125",
  "cliente": 15,
  "cliente_nombre": "Cliente Demo",
  "sucursal": 1,
  "sucursal_nombre": "Matriz",
  "envio": 5,
  "envio_transportista": 2,
  "envio_transportista_nombre": "Transportes Demo",
  "despacho_detalle": [
    {
      "id": 8,
      "despacho": 3,
      "packing_detalle": 21,
      "picking_detalle": 51,
      "pedido_detalle": 301,
      "pedido_detalle_talla": 990,
      "producto": 22,
      "producto_nombre": "Playera Dry Fit",
      "producto_variante": 91,
      "producto_variante_nombre": "Playera Dry Fit Negra M",
      "talla_id": 4,
      "talla_nombre": "M",
      "color_id": 2,
      "color_nombre": "Negro",
      "ubicacion": null,
      "ubicacion_nombre": null,
      "caja": null,
      "caja_numero": null,
      "cantidad_empacada": "3.0000",
      "estado": "PENDIENTE"
    }
  ]
}
```

**Notas para Next.js**

- El flujo recomendado es `GET /despachos/onboarding/` -> seleccionar `packing` -> seleccionar `envio` -> `POST /despachos/onboarding/`.
- Si todavía no existe `envio`, frontend puede crear el `despacho` solo con `packing` + `despacho_detalle` y asociar el envío después en otro paso operativo.
- El frontend no necesita recalcular si una línea ya fue despachada; el backend ya devuelve `ya_despachado` y `disponible_para_despacho`.
- `despacho` está ligado a `packing` y opcionalmente a `envio`; `despacho_detalle` está ligado a `packing_detalle`.

### 3) Listar Despachos

- **Endpoint**: `GET /api/v1/wms/despachos/`
- **Descripción**: devuelve los despachos visibles para la empresa y sucursales del usuario autenticado.

### 4) Detalle de Despacho

- **Endpoint**: `GET /api/v1/wms/despachos/{id}/`
- **Descripción**: devuelve encabezado y `despacho_detalle` del despacho registrado.

---

## 🏷️ WMS - Etiquetas RFID

Conector oficial para que Next.js genere etiquetas con ZPL (gráfico + barcode + EPC), y registre trazabilidad de cada impresión.
La fuente de verdad (producto, SKU, EPC, ZPL, usuario, impresora, estatus) vive en backend.
El frontend selecciona la impresora, envía el ZPL vía Zebra Browser Print y notifica el resultado.

**Integración del lado cliente (Zebra Browser Print)**

- **Origen y descarga**: Zebra Browser Print es utilería de Zebra Technologies instalada localmente en la estación de trabajo.
  - Instalador y documentación oficial: https://www.zebra.com/us/en/support-downloads/software/printer-software/browser-print.html
- **Librería cliente servida por el backend Django**:
  - Endpoint estático: `GET /QA/browserprint/BrowserPrint-3.1.250.min.js/`
  - Implementación: vista `qa_browserprint_asset` en [QA/views.py](file:///c:/Users/Jes%C3%BAs%20Ibarra/Desktop/django-backend-v2/QA/views.py#L512-L520), URL registrada en [QA/urls.py](file:///c:/Users/Jes%C3%BAs%20Ibarra/Desktop/django-backend-v2/QA/urls.py).
- **Modo de uso en Next.js**: cargar dicha librería en el contexto del modal y usar su API (`BrowserPrint.getDefaultDevice`, `device.send(zpl)`)
  para enviar cada ZPL directamente a la impresora detectada (USB / red). El nombre y dirección de la impresora que devuelve Browser Print
  se persisten en el backend a través del `POST onboarding`.

**Flujo onboarding recomendado (1 endpoint / 1 modal)**

```
ETAPAS DEL MISMO GET/POST:
  1) ABRIR MODAL SIN TEXTO
       GET  onboarding() → {"tiene_seleccion": false, "resultados": [...primeros registros sugeridos...]}

  2) USUARIO ESCRIBE SKU / TEXTO DE BUSQUEDA
       GET  onboarding(?q="RAYAS THAI") → {"tiene_seleccion":false, "resultados": [filtrados...]}
       → pinta la lista con el texto del campo "label"

  3) SELECCIONAR UN PRODUCTO/VARIANTE + DEFINIR CANTIDAD
       GET  onboarding(?variante=155&cantidad=5&rfid_mode=true)
       → RESPONSE CONTIENE PREVIEW COMPLETO CON UN ZPL INDIVIDUAL YA ARMADO POR ETIQUETA:
          {"tiene_seleccion": true,
           "preview": {
              "preview_data": {...},
              "zpl_individual": ["^XA...^FS...EPC1...^XZ", "^XA...EPC2...^XZ", ...],
              "etiquetas": [{"epc":"...","n":1,...}, ...]
            }
          }
       → el frontend itera sobre preview.zpl_individual[] y envía cada cadena
         a Browser Print vía `device.send(zpl)`.

  4) AL TERMINAR IMPRESION
       POST onboarding(
         {"producto_variante":155, "cantidad":5, "rfid_mode":true,
          "printer_name":"ZD621R-203dpi", "printer_address":"192.168.1.154",
          "status":"EXITO", "zpl_enviado":"<primer zpl>", "observaciones":"",
          "etiquetas": [opcional, mismo arreglo del preview con los EPC reales usados]
         }
       )
       → 201 CREATED → se crea el registro de impresión + sus detalles EPC.
       → el listado de impresiones `GET /api/v1/wms/etiquetas-rfid/` devuelve el nuevo registro.
```

---

### 0) Onboarding IMPRESIÓN RÁPIDA (modal 1 URL)

- **Endpoint**: `GET /api/v1/wms/etiquetas-rfid/onboarding/`
- **Alias de escritura**: `POST /api/v1/wms/etiquetas-rfid/onboarding/`
- **Descripción**: flujo único simple de 4 pasos (abrir / buscar / preview / imprimir / registrar).
  Internamente reúne: buscador de variantes/productos (mismos filtros Q de QA `imprimir_etiqueta_workspace`),
  preview de ZPL completo y registro de trazabilidad (misma escritura de `registrar-impresion`).

**Query params GET**

- `q`: texto libre, filtra buscador por SKU / nombre / código / cod_proscai.
- `variante` o `variante_id`: ID de `ProductoVariante` seleccionada. Al mandar este param, el GET devuelve `tiene_seleccion=true` + `preview` completo.
- `producto` o `producto_id`: ID de `Producto` si no hay variante.
- `cantidad`: entero opcional, default `1`.
- `rfid_mode`: boolean `true|false`, default `true`.

**Respuestas GET shape**

```json
// Caso 1) modal abierto sin seleccionar nada / solo busqueda
{
  "q": "",
  "resultados": [
    {
      "tipo": "variante",
      "id": 155,
      "producto_variante_id": 155,
      "producto_id": 91,
      "label": "10005032XC - CAMISA MANGA LARGA RAYAS THAI PREMIUM · VINO · 2XC",
      "sku": "10005032XC",
      "color_nombre": "VINO",
      "talla_nombre": "2XC"
    }
  ],
  "sucursal_ids": null,
  "tiene_seleccion": false,
  "preview": null
}

// Caso 2) ya seleccionó variante=155 & cantidad=3 → preview COMPLETO CON ZPLs listos
{
  "q": "",
  "resultados": [...resultados sugeridos...],
  "tiene_seleccion": true,
  "mensaje": "Next.js: iterar preview.zpl_individual[] y enviar cada ZPL a Browser Print. Al terminar hacer POST a este mismo endpoint.",
  "preview": {
    "cantidad": 3,
    "rfid_mode": true,
    "preview_data": { ... },
    "zpl_normal": "...",
    "zpl_rfid_first": "...",
    "zpl_individual": [
      "^XA...^FDEPC1...^XZ",
      "^XA...^FDEPC2...^XZ",
      "^XA...^FDEPC3...^XZ"
    ],
    "etiquetas": [
      {"n": 1, "epc": "...", "serial": "0001", "barcode_value": "10005032XC"},
      {"n": 2, "epc": "...", "serial": "0002", "barcode_value": "10005032XC"},
      {"n": 3, "epc": "...", "serial": "0003", "barcode_value": "10005032XC"}
    ]
  }
}
```

**POST onboarding (registrar impresión tras imprimir)**

Mismo save que `registrar-impresion` tradicional. Para que Next.js no tenga que armar nada raro,
aquí está el body mínimo listo para pegar del modal:

```json
{
  "producto_variante": 155,
  "cantidad": 3,
  "rfid_mode": true,
  "printer_name": "ZD621R-203dpi",
  "printer_address": "192.168.1.154",
  "status": "EXITO",
  "zpl_enviado": "^XA... (primer zpl que salió, opcional para auditoría)",
  "observaciones": ""
}
```

Campos opcionales avanzados (no necesarios para el flujo simple):

- `producto`: XOR con `producto_variante`, para productos base sin variantes.
- `etiquetas`: array de `{epc, barcode_value, serial}` con los EPC reales usados.
  **Si no lo mandas backend genera los EPCs automáticamente igual que el preview.**
- Si el frontend envía `etiquetas` debe traer exactamente `cantidad` renglones.

**Respuesta 201 de POST**: mismo shape que `GET /api/v1/wms/etiquetas-rfid/{id}/`.
La impresión ya quedó registrada y se verá en `GET /api/v1/wms/etiquetas-rfid/` (`/wms/rfid-labels` del ERP).

---

### 0-bis) Buscador de SKU / Producto (endpoint de bajo nivel; si no usas onboarding)

- **Endpoint**: `GET /api/v1/wms/etiquetas-rfid/buscar/`
- **Descripción**: buscador simple y ligero (mismo criterio Q que QA onboarding).
  El onboarding ya lo usa internamente, así que si armaste el modal de onboarding
  **no necesitas llamarlo aparte**. Está expuesto por si algún día hacen otra UI más granular.

---

### 1) Preview de Etiquetas

- **Endpoint**: `GET /api/v1/wms/etiquetas-rfid/preview/`
- **Descripción**: genera payload de preview para impresión de etiquetas (variante o producto base).
- **Autenticación**: sesión/Token del usuario; respeta empresa y sucursales permitidas.

**Query params**

- `variante` o `variante_id`: ID de `ProductoVariante` (si el producto usa variantes).
- `producto` o `producto_id`: ID de `Producto` (si no existen variantes).
- `cantidad`: opcional, entero, default `1`.
- `rfid_mode`: opcional, `true|false`, default `true`. Si `false`, solo gráfico + barcode.

**Ejemplo**

- `GET /api/v1/wms/etiquetas-rfid/preview/?variante=135&cantidad=2&rfid_mode=true`

**Respuesta resumida**

```json
{
  "empresa": 1,
  "sucursal": 1,
  "cantidad": 2,
  "rfid_mode": true,
  "producto": {
    "id": 10,
    "nombre": "CAMISA MANGA LARGA RAYAS THAI PREMIUM",
    "codigo": "1000",
    "cod_proscai": null
  },
  "producto_variante": {
    "id": 135,
    "sku": "1000700CH",
    "nombre": null,
    "color": "AZUL",
    "talla": "CH"
  },
  "preview_data": {
    "header": "SKU 1000700CH · CAMISA MANGA LARGA RAYAS THAI PREMIUM",
    "title": "CAMISA MANGA LARGA RAYAS THAI PREMIUM",
    "primary_line": "SKU: 1000700CH",
    "secondary_line": "AZUL / CH",
    "meta_line": "COD: 1000",
    "barcode_value": "1000700CH"
  },
  "zpl_normal": "^XA\\n^PW799\\n...^XZ",
  "zpl_rfid_first": "^XA\\n^PW799\\n...^RFW,E,,N\\n^FDE280...^FS\\n...^XZ",
  "etiquetas": [
    {
      "n": 1,
      "epc": "E2806894FFFFFFFF0001ABCD",
      "serial": "0001",
      "barcode_value": "1000700CH"
    },
    {
      "n": 2,
      "epc": "E2806894FFFFFFFF0002E123",
      "serial": "0002",
      "barcode_value": "1000700CH"
    }
  ]
}
```

**Notas**

- `zpl_normal`: ZPL sin RFID; solo gráfico y barcode. Útil para validar layout en impresoras no RFID.
- `zpl_rfid_first`: ejemplo completo de la **primera** etiqueta con EPC codificado. Útil como referencia, pero para imprimir `cantidad > 1` el frontend debe generar un ZPL por etiqueta, usando el `epc` de cada renglón en `etiquetas[]`.
- El backend valida acceso: si la variante/producto no pertenece a la empresa del usuario devuelve `400`.

---

### 2) Registrar Impresión (Trazabilidad)

- **Endpoint**: `POST /api/v1/wms/etiquetas-rfid/registrar-impresion/`
- **Alias**: `POST /api/v1/wms/etiquetas-rfid/`
- **Descripción**: guarda en DB el encabezado de impresión + detalle de cada EPC, para trazabilidad y para que después Fase 4 cruce estas etiquetas con lecturas en recepciones/picking/packing.
- **Autenticación**: sesión/Token del usuario.

**Body mínimo (backend genera EPCs automáticamente)**

```json
{
  "producto_variante": 135,
  "cantidad": 2,
  "rfid_mode": true,
  "printer_name": "ZD621R-203dpi",
  "printer_address": "192.168.1.154",
  "status": "EXITO",
  "zpl_enviado": "^XA...^XZ",
  "observaciones": "Impreso desde Next.js vía Browser Print"
}
```

**Body completo (frontend envía sus propios EPCs, recomendado para producción)**

```json
{
  "producto_variante": 135,
  "cantidad": 2,
  "rfid_mode": true,
  "printer_name": "ZD621R-203dpi",
  "printer_address": "192.168.1.154",
  "status": "EXITO",
  "zpl_enviado": "^XA...^XZ",
  "observaciones": "Reimpresión de etiquetas para pedido 125",
  "etiquetas": [
    {
      "epc": "E2806894FFFFFFFF0001ABCD",
      "barcode_value": "1000700CH",
      "serial": "0001"
    },
    {
      "epc": "E2806894FFFFFFFF0002E123",
      "barcode_value": "1000700CH",
      "serial": "0002"
    }
  ]
}
```

**Campos**

- `producto_variante` XOR `producto`: **obligatorio uno de los dos**.
- `cantidad`: opcional, default `1`.
- `rfid_mode`: opcional, default `true`. Si `false`, no se crean renglones en `etiquetas_rfid_detalle`.
- `printer_name`, `printer_address`: opcionales, útiles para auditoría.
- `status`: opcional, `PENDIENTE|EXITO|FALLIDO`, default `PENDIENTE`.
- `zpl_enviado`, `observaciones`: opcionales.
- `etiquetas[]`: opcional. Si lo envía el frontend, debe tener exactamente `cantidad` renglones y cada uno su `epc`. Si no lo envía, backend genera los EPCs automáticamente.

**Respuesta 201**

```json
{
  "id": 42,
  "folio": "LAB-000042",
  "empresa": 1,
  "sucursal": 1,
  "usuario": 5,
  "producto": 10,
  "producto_variante": 135,
  "producto_nombre": "CAMISA MANGA LARGA RAYAS THAI PREMIUM",
  "producto_variante_nombre": null,
  "sku": "1000700CH",
  "codigo_producto": "1000",
  "cantidad": 2,
  "rfid_mode": true,
  "printer_name": "ZD621R-203dpi",
  "printer_address": "192.168.1.154",
  "status": "EXITO",
  "observaciones": "Impreso desde Next.js vía Browser Print",
  "created_at": "2026-07-31T20:30:00Z",
  "etiquetas": [
    {
      "id": 101,
      "impresion": 42,
      "epc": "E2806894FFFFFFFF0001ABCD",
      "barcode_value": "1000700CH",
      "serial": "0001",
      "estado": "IMPRESO"
    }
  ]
}
```

---

### 3) Listar Impresiones

- **Endpoint**: `GET /api/v1/wms/etiquetas-rfid/`
- **Descripción**: devuelve encabezado + `etiquetas` (detalle EPC) de las impresiones visibles para la empresa/sucursales del usuario.

### 4) Detalle de Impresión

- **Endpoint**: `GET /api/v1/wms/etiquetas-rfid/{id}/`
- **Descripción**: detalle completo de una impresión, incluyendo sus EPCs individuales.

---

### 5) Checklist de integración Next.js

| Paso | Acción                                                                                                                                                                                                                                                                                |
| ---- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1    | Abrir modal y consultar `GET /api/v1/wms/etiquetas-rfid/onboarding/` para obtener resultados iniciales.                                                                                                                                                                               |
| 2    | Buscar texto: `GET /api/v1/wms/etiquetas-rfid/onboarding/?q=<texto>`. Renderizar la lista con el campo `label`.                                                                                                                                                                       |
| 3    | Seleccionar variante/producto y cantidad, consultar `GET /onboarding/?variante=X&cantidad=N&rfid_mode=true` (o `?producto=Y`). Usar `preview.zpl_individual[]` como fuente de ZPL.                                                                                                    |
| 4    | Cargar Zebra Browser Print desde `GET /QA/browserprint/BrowserPrint-3.1.250.min.js/`, detectar impresora (`BrowserPrint.getDefaultDevice` o listado de dispositivos) y enviar cada ZPL individual con `device.send(zpl)`.                                                             |
| 5    | Al finalizar el envío de las etiquetas, registrar la operación con `POST /api/v1/wms/etiquetas-rfid/onboarding/`: `producto_variante`, `cantidad`, `rfid_mode`, `printer_name`, `printer_address`, `status`. Enviar opcionalmente el arreglo `etiquetas[]` con los EPC reales usados. |
| 6    | Actualizar el listado de impresiones en pantalla a partir de `GET /api/v1/wms/etiquetas-rfid/`.                                                                                                                                                                                       |

---

## 🛰️ WMS - Scanner / Lector RFID (Next.js)

Lector oficial para consumir lecturas desde el hardware Zebra FX (FX7500 / FX9600) y **hacer MATCH automático** contra las etiquetas impresas (EtiquetaRFIDDetalle).
El frontend (Next.js) NO habla directamente con el lector FX. El flujo es:

```
  LECTOR FX ZEBRA  ──POST JSON/EPCs──▶  /QA/scanner_rfid/receive/  ──▶ DB: RfidScan
                                                                          │
  NEXT.JS  ──poll GET cada 2s──▶  /api/v1/wms/etiquetas-rfid/scans/  ◀──┘
                                  (match auto con detalle)
```

**Importante arquitectura**:

- El `POST /QA/scanner_rfid/receive/` es SOLO para el FX (no requiere token / `@csrf_exempt`). **Next.js NUNCA llama a receive**.
- Next.js solo consume 3 endpoints del V1 (mismo Bearer token que el resto del ERP):
  1. `GET /api/v1/wms/etiquetas-rfid/scans/` → polling (lista lecturas + MATCH)
  2. `GET /api/v1/wms/etiquetas-rfid/scanner-stats/` → debug 1-clic (estado FX + busqueda ?epc=)
  3. `POST /api/v1/wms/etiquetas-rfid/scans/clear/` → purge list (vaciar tabla)

---

### 0) Onboarding SCANNER (1 modal simple)

No hay endpoint especial tipo onboarding. El modal consta de 3 partes:

| UI Element                                 | Código a ejecutar                                                        |
| ------------------------------------------ | ------------------------------------------------------------------------ |
| Botón **🔄 Iniciar Monitoreo** cada 2s     | `setInterval()` llamando `GET /api/v1/wms/etiquetas-rfid/scans/`         |
| Botón **🗑️ Purge List** (antes de empezar) | `POST /api/v1/wms/etiquetas-rfid/scans/clear/`                           |
| Botón **🛠️ Ver Status FX** (debug 1-clic)  | `GET /api/v1/wms/etiquetas-rfid/scanner-stats/?epc=<EPC_RECIEN_IMPRESO>` |

---

### 1) Polling de Lecturas (principal / polling cada 2s)

- **Endpoint**: `GET /api/v1/wms/etiquetas-rfid/scans/`
- **Descripción**: devuelve **últimas 50 filas de `RfidScan`** en orden DESC (reciente → antiguo). **Cada scan tiene ya calculado el `match_impresion`** (true/false) contra `EtiquetaRFIDDetalle`, con scope empresa/sucursales.
- **Autenticación**: Bearer token.
- **Query params (opcionales para debug)**:
  - `epc=XXXX` (hex): busca directo este EPC en el payload de 50 scans y devuelve `debug_get.query_epc_search.found_in_scans` (booleano rápido sin iterar).

**Ejemplo URL de debug (recién impresa una etiqueta)**:

```
GET /api/v1/wms/etiquetas-rfid/scans/?epc=000012E32827000147C0C5F5
```

**Respuesta shape (mínimo a renderizar)**:

```json
{
  "scans": [
    {
      "id": 3856,
      "epc": "000012e32827000147c0c5f5",
      "timestamp": "2026-08-07T19:51:45.490950+00:00",
      "antenna": 1,
      "rssi": -45.0,
      "reader_ip": "187.188.149.179",

      "match_impresion": true,

      "impresion_folio": "LAB-000022",
      "impresion_id": 22,
      "producto_nombre": "CAMISA MANGA LARGA RAYAS THAI PREMIUM VINO",
      "sku": "1000503G",
      "color": "VINO",
      "talla": "G",
      "barcode_value": "1000503G",
      "serial": "0001",
      "estado": "IMPRESO",
      "detalle_id": 62,
      "match_debug": {
        "scan_epc": "000012e32827000147c0c5f5",
        "scan_epc_len": 24,
        "variants_tried": ["12e32827000147c0c5f5", "000012e32827000147c0c5f5"],
        "variant_used": "12e32827000147c0c5f5",
        "detalle_epc_raw": "000012E32827000147C0C5F5",
        "detalle_epc_len": 24,
        "detalle_epc_variants": ["000012e32827000147c0c5f5", "12e32827000147c0c5f5"]
      }
    },

    {
      "id": 3827,
      "epc": "3035c9d34c5767c0004ccc4c",
      "timestamp": "...",
      "antenna": null,
      "rssi": null,
      "reader_ip": "187.188.149.179",
      "match_impresion": false,
      "match_debug": {
        "scan_epc": "3035c9d34c5767c0004ccc4c",
        "scan_epc_len": 24,
        "variants_tried": ["3035c9d34c5767c0004ccc4c", ...],
        "variant_used": null,
        "detalle_lookup_count": 2
      }
    }
  ],

  "debug_get": {
    "scans_returned": 2,
    "scans_total_max_50": 2,
    "lookup_detalle_count": 2,
    "unique_epc_in_50_scans_count": 2,
    "unique_epc_prefixes_head30": ["0000", "3035"],
    "query_epc_search": {
      "query_epc": "000012e32827000147c0c5f5",
      "query_epc_len": 24,
      "variants_count": 2,
      "variants_head5": ["000012e32827000147c0c5f5", "12e32827000147c0c5f5"],
      "found_in_scans": true,
      "hit_variant": "000012e32827000147c0c5f5"
    }
  }
}
```

**Reglas de render UI**:

- Si `match_impresion === true` → fila en VERDE, muestra sku, color, talla, folio LAB-000XX.
- Si `match_impresion === false` → fila en ROJO / GRIS, muestra EPC en hex crudo.
- Usa `antenna` (int 1..8) y `rssi` (dBm, valor negativo: -30 bueno / -70 muy débil) como métricas de señal.
- `timestamp`: ISO string; mostrar hora local.

---

### 2) Ejemplo Next.js: Componente mínimo Scanner (copy-paste funcional)

```tsx
// app/wms/rfid-scanner/page.tsx
"use client";

import { useEffect, useMemo, useRef, useState } from "react";

type RfidScan = {
  id: number;
  epc: string;
  timestamp: string;
  antenna: number | null;
  rssi: number | null;
  reader_ip: string | null;
  match_impresion: boolean;
  impresion_folio?: string | null;
  sku?: string | null;
  color?: string | null;
  talla?: string | null;
  producto_nombre?: string | null;
  barcode_value?: string | null;
  serial?: string | null;
  detalle_id?: number | null;
  match_debug?: Record<string, unknown>;
};

const BASE = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8003";

function authHeaders(token: string): Record<string, string> {
  return {
    Authorization: `Bearer ${token}`,
    "Content-Type": "application/json",
  };
}

export default function RfidScannerPage() {
  const token = "tu-token-jwt"; // reemplaza: getToken() de tu sesión
  const [running, setRunning] = useState(false);
  const [scans, setScans] = useState<RfidScan[]>([]);
  const lastSeenId = useRef(0);
  const timer = useRef<ReturnType<typeof setInterval> | null>(null);

  const uniqueByEpc = useMemo(() => {
    const seen = new Map<string, RfidScan>();
    for (const s of [...scans].sort((a, b) => b.id - a.id)) seen.set(s.epc, s);
    return Array.from(seen.values());
  }, [scans]);

  async function fetchOnce() {
    const res = await fetch(`${BASE}/api/v1/wms/etiquetas-rfid/scans/`, {
      headers: authHeaders(token),
    });
    const json = await res.json();
    const list: RfidScan[] = json.scans ?? [];
    const nuevos = list.filter((s) => s.id > lastSeenId.current);
    if (nuevos.length > 0) {
      setScans((prev) => [...nuevos, ...prev].slice(0, 200));
      lastSeenId.current = Math.max(
        lastSeenId.current,
        ...nuevos.map((s) => s.id),
      );
    }
  }

  async function purgeList() {
    await fetch(`${BASE}/api/v1/wms/etiquetas-rfid/scans/clear/`, {
      method: "POST",
      headers: authHeaders(token),
    });
    lastSeenId.current = 0;
    setScans([]);
  }

  function toggle() {
    setRunning((prev) => {
      const next = !prev;
      if (next) {
        timer.current = setInterval(fetchOnce, 2000);
        fetchOnce();
      } else if (timer.current) {
        clearInterval(timer.current);
        timer.current = null;
      }
      return next;
    });
  }

  useEffect(
    () => () => {
      if (timer.current) clearInterval(timer.current);
    },
    [],
  );

  return (
    <main className="p-6 max-w-6xl mx-auto space-y-4">
      <div className="flex gap-3">
        <button
          onClick={toggle}
          className="px-4 py-2 rounded bg-blue-600 text-white"
        >
          {running ? "Detener Monitoreo" : "Iniciar Monitoreo (2s)"}
        </button>
        <button
          onClick={purgeList}
          className="px-4 py-2 rounded bg-red-500 text-white"
        >
          Purge List
        </button>
      </div>

      <div className="text-sm text-gray-600">
        Últimas lecturas: {scans.length} rows · Únicas por EPC:{" "}
        {uniqueByEpc.length}
      </div>

      <table className="w-full text-sm border">
        <thead>
          <tr className="bg-gray-100">
            <th className="px-3 py-2 text-left">ID</th>
            <th className="px-3 py-2 text-left">MATCH</th>
            <th className="px-3 py-2 text-left">EPC</th>
            <th className="px-3 py-2 text-left">SKU · Color · Talla</th>
            <th className="px-3 py-2 text-left">Folio</th>
            <th className="px-3 py-2 text-left">ANT</th>
            <th className="px-3 py-2 text-left">RSSI</th>
            <th className="px-3 py-2 text-left">Timestamp</th>
          </tr>
        </thead>
        <tbody>
          {uniqueByEpc.map((s) => (
            <tr
              key={s.id}
              className={s.match_impresion ? "bg-green-50" : "bg-red-50"}
            >
              <td className="px-3 py-2">{s.id}</td>
              <td className="px-3 py-2 font-semibold">
                {s.match_impresion ? "✅ SI" : "❌ NO"}
              </td>
              <td className="px-3 py-2 font-mono text-xs">{s.epc}</td>
              <td className="px-3 py-2">
                {s.match_impresion
                  ? `${s.sku} · ${s.color ?? ""} · ${s.talla ?? ""}`
                  : "—"}
              </td>
              <td className="px-3 py-2">{s.impresion_folio ?? "—"}</td>
              <td className="px-3 py-2">{s.antenna ?? "—"}</td>
              <td className="px-3 py-2">{s.rssi ?? "—"}</td>
              <td className="px-3 py-2">
                {new Date(s.timestamp).toLocaleString()}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </main>
  );
}
```

---

### 3) Endpoint Debug Status FX (1 clic, sin entrar Vercel)

Útil para:

- Saber **cuándo fue la última lectura del FX** (`last_scan_seconds_ago`: si es `> 300` = FX está apagado / no conectado a internet).
- Buscar **rápido si una etiqueta recién impresa fue leída** por el FX sin abrir el admin Django.

- **Endpoint**: `GET /api/v1/wms/etiquetas-rfid/scanner-stats/`
- **Query params**:
  - `epc=XXXX` (opcional): EPC hex de una etiqueta recién impresa para buscarlo directo en la DB.

**Ejemplo**:

```
GET /api/v1/wms/etiquetas-rfid/scanner-stats/?epc=000012E32827000147C0C5F5
```

**Respuesta shape**:

```json
{
  "status": "ok",
  "total_rfidscan_rows": 1,
  "last_scan_ts": "2026-08-07T19:51:45.490950+00:00",
  "last_scan_seconds_ago": 26,
  "last_5_scans": [
    {
      "id": 3856,
      "epc": "000012e32827000147c0c5f5",
      "epc_len": 24,
      "antenna": 1,
      "rssi": -45,
      "reader_ip": "187.188.149.179",
      "ts": "..."
    }
  ],
  "query_epc": "000012e32827000147c0c5f5",
  "query_epc_found_count": 1,
  "query_epc_found_samples": [
    {
      "id": 3856,
      "epc": "...",
      "epc_len": 24,
      "antenna": 1,
      "rssi": -45,
      "ts": "..."
    }
  ],
  "receive_endpoint_info": {
    "fx_post_url_required": "POST https://TU-BACKEND/QA/scanner_rfid/receive/ (FX llama aquí. Next.js NO)",
    "method_required": "POST (FX no manda token; @csrf_exempt).",
    "note": "Next.js solo consume scans/, scans/clear y scanner-stats."
  }
}
```

---

### 4) Purge List (vaciar lecturas antes de una prueba)

- **Endpoint**: `POST /api/v1/wms/etiquetas-rfid/scans/clear/`
- **Body**: ninguno (vacío)
- **Respuesta**:
  ```json
  { "status": "success", "deleted": 42 }
  ```

---

### 5) Checklist Integración Scanner Next.js

| Paso | Acción                                                                                                                                                                                                         |
| ---- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1    | Configura en el LECTOR FX Zebra (FX7500/FX9600) su **POST URL**: `https://TU-BACKEND/QA/scanner_rfid/receive/` (content-type JSON).                                                                            |
| 2    | (Antes de empezar) Click **Purge List** = `POST /api/v1/wms/etiquetas-rfid/scans/clear/`.                                                                                                                      |
| 3    | Click **Iniciar Monitoreo** = `setInterval` cada **2000 ms (2s)** llamando `GET /api/v1/wms/etiquetas-rfid/scans/`. Usa `lastSeenId` (Ref) para agregar solo scans nuevos (ids mayores).                       |
| 4    | Render tabla: columna `MATCH=✅/❌` + `sku`, `color`, `talla`, `folio`, `antenna`, `rssi`, `timestamp`. Si `match_impresion=false` mostrar EPC hex crudo; si `true` pintar fila VERDE con los campos producto. |
| 5    | Debug rápido: ante duda click **Status FX** = `GET /scanner-stats/?epc=<EPC_IMPRESO>` y revisa `query_epc_found_count` (0 no leída, ≥1 leída) + `last_scan_seconds_ago` (>300s = FX offline).                  |

---

## 🧪 QA RFID Workspace

Flujo de pruebas locales para validar impresión Zebra y captura de lecturas desde dispositivos Zebra sin afectar inventario.

### 1) Workspace de Recepciones RFID

- **URL**: `GET /QA/rfid/recepciones/`
- **Descripción**: permite crear un encuadre QA, registrar lecturas y comparar lo esperado vs lo leído antes de pasar a una recepción formal.

#### Query params

- `encuadre` opcional: abre un encuadre existente para seguir escaneando.

#### Notas operativas

- El flujo QA no genera movimientos de stock.
- La captura actual acepta valores por `sku`, `codigo` o `cod_proscai`.
- Si el código no se puede resolver, la lectura queda registrada como no asignada.

### 2) Workspace de Impresión QA

- **URL**: `GET /QA/imprimir_etiqueta/`
- **Alias tolerado**: `GET /QA/imrpimir_etiqueta/`
- **Descripción**: busca variantes o productos base, genera un ZPL de prueba y permite imprimirlo vía Zebra Browser Print local.

#### Query params

- `q` opcional: busca por `sku`, nombre, `codigo` o `cod_proscai`.
- `variante` opcional: selecciona una `ProductoVariante`.
- `producto` opcional: selecciona un `Producto` cuando no existen variantes.
- `encuadre` opcional: conserva el retorno al workspace de recepción QA.

#### Comportamiento

- Si la búsqueda encuentra variantes, se puede imprimir por `sku`.
- Si la búsqueda encuentra un producto sin variantes, se puede imprimir por `codigo` o `cod_proscai`.
- Si solo existe un producto coincidente y no hay variantes, la pantalla lo selecciona automáticamente.
- El frontend carga Browser Print desde rutas QA dedicadas para no depender de `collectstatic` durante pruebas locales.

#### Assets locales usados por Browser Print

- `GET /QA/browserprint/BrowserPrint-3.1.250.min.js`
- `GET /QA/browserprint/BrowserPrint-Zebra-1.1.250.min.js`

### 3) Flujo validado en pruebas

1. Crear o abrir un encuadre en `GET /QA/rfid/recepciones/?encuadre={id}`.
2. Ir a `GET /QA/imprimir_etiqueta/?encuadre={id}`.
3. Buscar una variante o un producto base, por ejemplo `93E0`.
4. Imprimir la etiqueta desde la PC con Zebra Browser Print.
5. Abrir el encuadre en el Zebra `MC3300X`.
6. Escanear el código impreso para registrar la lectura en QA.

### 4) Alcance actual

- La validación completada en QA corresponde a lectura por código de barras enviado por el Zebra como entrada de teclado.
- La lectura RFID real será una fase posterior, donde el dispositivo deberá enviar `EPC` u otro identificador RFID al backend.

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

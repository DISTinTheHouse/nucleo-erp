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
  - `tipo_movimiento`: requerido, `ENTRADA`, `SALIDA` o `AJUSTE`
  - `fecha_inicio`: requerido, formato `YYYY-MM-DD`
  - `fecha_final`: requerido, formato `YYYY-MM-DD`
  - `almacen_id`: opcional; si no se envía, el reporte incluye todos los almacenes visibles para el usuario
- **Ejemplo**:
  - `GET /api/v1/inventarios/movimientos/reporte-movimientos-periodo/?tipo_movimiento=SALIDA&fecha_inicio=2026-07-01&fecha_final=2026-07-31&almacen_id=1`
  - `GET /api/v1/inventarios/movimientos/reporte-movimientos-periodo/?tipo_movimiento=SALIDA&fecha_inicio=2026-07-01&fecha_final=2026-07-31`
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
  - conserva el bloque `detalles` de la orden de compra
  - agrega el bloque `recepciones` con todas las recepciones activas ligadas a esa `orden_compra`
  - cada recepción incluye su información general y su `detalles`

**Respuesta (resumen)**

```json
{
  "id": 112,
  "folio": "OC-000112",
  "estatus": 4,
  "estatus_label": "Parcialmente recibida",
  "detalles": [
    {
      "producto_id": 1,
      "descripcion": "Tela gabardina",
      "cantidad": 10,
      "precio": "150.00",
      "importe": "1500.00"
    }
  ],
  "recepciones": [
    {
      "id": 35,
      "tipo_origen": "OC",
      "folio": "RC-000035",
      "remision": "REM-102",
      "factura_referencia": "FAC-9001",
      "fecha_recepcion": "2026-07-07T12:00:00Z",
      "estatus": 2,
      "estatus_label": "Recibida",
      "sucursal": 1,
      "sucursal_nombre": "Matriz",
      "proveedor": 8,
      "proveedor_nombre": "Proveedor Demo",
      "almacen": 3,
      "almacen_nombre": "Almacen General",
      "transportista": null,
      "transportista_nombre": null,
      "observaciones": "Recepcion parcial",
      "detalles": [
        {
          "id": 77,
          "orden_compra_detalle": 25,
          "producto": 1,
          "producto_nombre": "Tela gabardina",
          "producto_variante": null,
          "ubicacion": 12,
          "ubicacion_nombre": "Rack A1",
          "lote": null,
          "serie": null,
          "cantidad_recibida": "4.0000"
        }
      ]
    }
  ]
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

### 7) Orden de Bordado Onboarding (patrón sencillo / manual)

- **Endpoints**:
  - `GET /api/v1/produccion/orden-bordado/onboarding/`
  - `POST /api/v1/produccion/orden-bordado/onboarding/`
- **Objetivo**: patrón onboarding idéntico a WMS (picking / packing / despacho): catálogos precargados para que Next.js muestre selector de pedido + operadores + preview folio.

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
      "sucursal_nombre": "Matriz"
    }
  ],
  "operadores": [{ "id": 8, "nombre": "Juan Pérez" }],
  "preview": {
    "folio_ob_sugerido": "OB-20260730-001"
  }
}
```

**Reglas del GET onboarding**

| Campo                                  | Regla                                                                                                                                                              |
| -------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `pedidos`                              | Solo pedidos con al menos una `PedidoDetalleTalla` con `lleva_bordado=True`. Scope por `empresa` + `sucursales_permitidas()` del usuario.                          |
| `operadores`                           | `Usuarios` activos de la empresa ordenados por nombre/email.                                                                                                       |
| `preview.folio_ob_sugerido`            | Usa SSoT `SerieFolio.preview_siguiente_folio()` (mismo modelo `nucleo.models.SerieFolio`). **Preview SIN consumo** (no gasta folio, no incrementa `folio_actual`). |
| Sin empresa / sin sucursales asignadas | Devuelve listas vacías `[]` sin error.                                                                                                                             |

**POST onboarding**

- **Mismo save que `create` tradicional** — usa el `OrdenBordadoSerializer` estándar.
- Body requerido mínimo: `{ "pedido": 125 }`.
- Opcionales: `prioridad`, `observaciones`.
- Internamente: carga automáticamente **todas** las `PedidoDetalleTalla` del pedido con `lleva_bordado=True`, genera folio OB único y `bulk_create` de `OrdenBordadoDetalle` con la cantidad 100% de cada línea.
- No depende de WMS ni de un picking existente; se genera completamente desde Producción.

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
      "cantidad": 10.0
    }
  ]
}
```

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

**Estados y cancelación**: Si se requiere reprocesar un pedido porque la OB original se canceló o cerró parcialmente, el `POST` volverá a permitir crear una nueva OB sin conflictos.

### 8) Orden de Reflejante Onboarding (patrón sencillo / manual)

- **Endpoints**:
  - `GET /api/v1/produccion/orden-reflejante/onboarding/`
  - `POST /api/v1/produccion/orden-reflejante/onboarding/`
- **Objetivo**: patrón onboarding idéntico a ÓrdenesBordado y WMS: catálogos precargados para que Next.js muestre selector de pedido + operadores + preview folio.

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
      "sucursal_nombre": "Matriz"
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
| `operadores`                           | `Usuarios` activos de la empresa ordenados por nombre/email.                                                                                                       |
| `preview.folio_or_sugerido`            | Usa SSoT `SerieFolio.preview_siguiente_folio()` (mismo modelo `nucleo.models.SerieFolio`). **Preview SIN consumo** (no gasta folio, no incrementa `folio_actual`). |
| Sin empresa / sin sucursales asignadas | Devuelve listas vacías `[]` sin error.                                                                                                                             |

**POST onboarding**

- **Mismo save que `create` tradicional** — usa el `OrdenReflejanteSerializer` estándar.
- Body requerido mínimo: `{ "pedido": 125 }`.
- Opcionales: `prioridad`, `observaciones`.
- Internamente: carga automáticamente **todas** las `PedidoDetalleTalla` del pedido con `lleva_reflejante=True`, genera folio OR único y `bulk_create` de `OrdenReflejanteDetalle` con la cantidad 100% de cada línea.
- No depende de WMS ni de un picking existente; se genera completamente desde Producción.

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
      "cantidad": 25.0
    }
  ]
}
```

**Control anti-duplicado (HTTP 409 Conflict)**

> Regla SSoT de negocio: no se permite crear más de una **OrdenesReflejante activa** para el mismo pedido si este ya cubre el 100% de las prendas con `lleva_reflejante=True`. Evita doble consumo de folio OR y doble programación en taller.

- **Trigger**: segundo `POST /api/v1/produccion/orden-reflejante/onboarding/` con el mismo `pedido` y la primera OR aún activa (no cancelada).
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

- **Garantía**: el consecutivo de `SerieFolio` para OrdenesReflejante **no se consume** cuando responde 409. Antes del gasto transaccional de folio corre `OrdenReflejanteService._validar_contexto` que incluye:
  - Validación **cross-tenant**: `pedido.empresa_id == user.empresa_id` y acceso por `sucursales_permitidas()`; si no, retorna error y no gasta folio.
  - `buscar_existente_full_match()`: detecta OR activa para el mismo pedido con cobertura 100%.

**Estados y cancelación**: Si se requiere reprocesar un pedido porque la OR original se canceló o cerró parcialmente, el `POST` volverá a permitir crear una nueva OR sin conflictos.

### 9) Orden de Corte de Manga Onboarding (patrón sencillo / manual)

- **Endpoints**:
  - `GET /api/v1/produccion/orden-corte-manga/onboarding/`
  - `POST /api/v1/produccion/orden-corte-manga/onboarding/`
- **Objetivo**: patrón onboarding idéntico a ÓrdenesBordado/ÓrdenesReflejante y WMS: catálogos precargados para que Next.js muestre selector de pedido + operadores + preview folio.

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
      "sucursal_nombre": "Matriz"
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
| `operadores`                           | `Usuarios` activos de la empresa ordenados por nombre/email.                                                                                                       |
| `preview.folio_ocm_sugerido`           | Usa SSoT `SerieFolio.preview_siguiente_folio()` (mismo modelo `nucleo.models.SerieFolio`). **Preview SIN consumo** (no gasta folio, no incrementa `folio_actual`). |
| Sin empresa / sin sucursales asignadas | Devuelve listas vacías `[]` sin error.                                                                                                                             |

**POST onboarding**

- **Mismo save que `create` tradicional** — usa el `OrdenesCorteMangaSerializer` estándar.
- Body requerido mínimo: `{ "pedido": 125 }`.
- Opcionales: `prioridad`, `observaciones`.
- Internamente: carga automáticamente **todas** las `PedidoDetalleTalla` del pedido con `lleva_corte_manga=True`, genera folio OCM único y `bulk_create` de `OrdenCorteMangaDetalle` con la cantidad 100% de cada línea.
- No depende de WMS ni de un picking existente; se genera completamente desde Producción.

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
      "cantidad": 40.0
    }
  ]
}
```

**Control anti-duplicado (HTTP 409 Conflict)**

> Regla SSoT de negocio: no se permite crear más de una **OrdenesCorteManga activa** para el mismo pedido si este ya cubre el 100% de las prendas con `lleva_corte_manga=True`. Evita doble consumo de folio OCM y doble programación en taller.

- **Trigger**: segundo `POST /api/v1/produccion/orden-corte-manga/onboarding/` con el mismo `pedido` y la primera OCM aún activa (no cancelada).
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

- **Garantía**: el consecutivo de `SerieFolio` para OrdenesCorteManga **no se consume** cuando responde 409. Antes del gasto transaccional de folio corre `OrdenCorteMangaService._validar_contexto` que incluye:
  - Validación **cross-tenant**: `pedido.empresa_id == user.empresa_id` y acceso por `sucursales_permitidas()`; si no, retorna error y no gasta folio.
  - `buscar_existente_full_match()`: detecta OCM activa para el mismo pedido con cobertura 100%.

**Estados y cancelación**: Si se requiere reprocesar un pedido porque la OCM original se canceló o cerró parcialmente, el `POST` volverá a permitir crear una nueva OCM sin conflictos.

---

## 📦 WMS - Picking

### 1) Onboarding de Picking

- **Endpoint**: `GET /api/v1/wms/pickings/onboarding/`
- **Objetivo**: dar al frontend todo lo necesario para preparar un surtido parcial o total mediante un flujo tipo onboarding de 4 pasos.

**Flujo onboarding**

1. Seleccionar el **pedido** (catálogo `pedidos`).
2. Seleccionar **almacén origen** (de dónde se tomarán las prendas; catálogo `almacenes`).
3. Seleccionar **almacén destino** (hacia dónde se moverá la mercancía; por defecto `APARTADOS`).
4. Vista previa del encabezado (`header` con fecha sugerida y folio preview) + líneas con **existencia disponible** para picking parcial y flags de órdenes de trabajo (bordado / reflejante / corte de manga).

**Query params**

| Param                                    | Requerido | Descripción                                                                    |
| ---------------------------------------- | --------- | ------------------------------------------------------------------------------ |
| `pedido` / `pedido_id`                   | No        | Activa la precarga del pedido y sus líneas de talla.                           |
| `almacen_origen` / `almacen_origen_id`   | No        | Preselecciona el almacén de origen; usado para calcular existencia disponible. |
| `almacen_destino` / `almacen_destino_id` | No        | Preselecciona el almacén destino (si no se envía se sugiere `APARTADOS`).      |

**Ejemplo**

- `GET /api/v1/wms/pickings/onboarding/?pedido_id=125&almacen_origen=3&almacen_destino=10`

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
      "sucursal": 1
    },
    {
      "id": 10,
      "codigo": "APTOS",
      "nombre": "APARTADOS",
      "sucursal": 1
    }
  ],
  "almacen_origen": {
    "id": 3,
    "codigo": "PT-MTY",
    "nombre": "Almacén PT Monterrey",
    "sucursal": 1
  },
  "almacen_destino": {
    "id": 10,
    "codigo": "APTOS",
    "nombre": "APARTADOS",
    "sucursal": 1
  },
  "header": {
    "fecha_picking_sugerida": "2026-07-28T15:30:00.123456Z",
    "folio_sugerido_preview": "PICK-000021"
  },
  "pedido": {
    "id": 125,
    "folio": "PD-000125",
    "cliente": 15,
    "cliente_nombre": "Cliente Demo",
    "sucursal": 1,
    "sucursal_nombre": "Matriz"
  },
  "picking_detalle": [
    {
      "pedido_detalle": 301,
      "pedido_detalle_talla": 990,
      "producto": 22,
      "producto_nombre": "Playera Dry Fit",
      "producto_variante": 91,
      "producto_variante_nombre": "Playera Dry Fit - Negro - M",
      "talla": 4,
      "talla_nombre": "M",
      "color": 2,
      "color_nombre": "Negro",
      "cantidad_pedida": "50.0000",
      "cantidad_ya_asignada": "0.0000",
      "cantidad_ya_surtida": "0.0000",
      "cantidad_pendiente": "50.0000",
      "existencia_fisica": "45.0000",
      "existencia_reservada": "15.0000",
      "existencia_disponible": "30.0000",
      "maximo_picking_permitido": "30.0000",
      "requiere_bordado": true,
      "requiere_reflejante": false,
      "requiere_corte_manga": false,
      "bordado_config": {
        "posicion": "PECHO",
        "colores_hilo": 3,
        "puntadas": 15000
      },
      "reflejante_config": null,
      "corte_manga_config": null
    }
  ]
}
```

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
- **Objetivo (modelo tradicional)**: **solo crear el documento** `Picking` + `PickingDetalle` con su folio único. Este paso no mueve inventario, no crea transferencias, no crea reservas ni órdenes de producción.

**Qué hace el POST (3 pasos)**

1. Next.js envía encabezado + `picking_detalle` con las cantidades reales a surtir por línea/talla.
2. El backend valida:
   - `cantidad_asignada` ≤ `cantidad_pendiente` de la talla.
   - `cantidad_asignada` ≤ `existencia_disponible` en el almacén origen (comparación contra existencia agregada por clave de stock).
   - **Validación agregada por clave**: la suma de `cantidad_asignada` de todas las líneas que comparten la misma clave `(producto_id, variante_id)` (incluyendo `variante_id = null`) debe ser ≤ la existencia disponible agregada de esa clave. Previene que múltiples tallas de un mismo producto sin variante agoten colectivamente el stock.
3. Crea el documento `Picking` + `PickingDetalle` (bulk_create), genera folio único y responde el picking.

> La **operación física** (tomar prendas del almacén origen y depositarlas en el destino) queda **fuera de este endpoint**. Para el movimiento de inventario se usan los endpoints del módulo Transferencias; para órdenes de producción se usa el módulo Producción.

**Body**

```json
{
  "pedido": 125,
  "operador": 8,
  "almacen": 3,
  "almacen_destino": 10,
  "prioridad": "MEDIA",
  "tipo": "ORDER_PICKING",
  "observaciones": "Surtido parcial 30 pz (restante en próximo picking)",
  "picking_detalle": [
    {
      "pedido_detalle_talla": 990,
      "cantidad_asignada": "30.0000",
      "observaciones": "Bordado especial — pasar a Produccion después"
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

**Campos opcionales / low-noise**

- `almacen_destino`: si no se envía, se resuelve automáticamente el almacén `APARTADOS` de la misma empresa + sucursal.
- `prioridad`: `BAJA`, `MEDIA`, `ALTA`
- `tipo`: `ORDER_PICKING`, `BATCH_PICKING`, `WAVE_PICKING`, `ZONE_PICKING`
- `oleada`, `zona_almacen`, `lote`
- `fecha_inicio`, `fecha_fin`, `fecha_limite`
- `observaciones`
- Por cada línea en `picking_detalle`:
  - `generar_orden_bordado` / `generar_orden_reflejante` / `generar_orden_corte_manga`: **aceptados pero IGNORADOS en el v1 de create**. Las órdenes de trabajo se generan desde Produccion endpoints dedicados. No se rechazan para mantener bajo ruido con Next.js.
  - `observaciones`

**Validaciones principales**

- El pedido debe pertenecer a la empresa del usuario.
- El almacén origen y destino deben pertenecer a la misma empresa y sucursal del pedido.
- El almacén origen y destino **no** pueden ser el mismo.
- El operador debe estar activo y pertenecer a la misma empresa.
- Cada renglón debe incluir `pedido_detalle_talla` y `cantidad_asignada > 0`.
- Cada cantidad enviada **no** puede exceder lo pendiente del pedido para esa talla.
- Cada cantidad enviada **no** puede exceder la `existencia_disponible` en el almacén origen.
- Si `almacen_destino` no se envía, debe existir un almacén `APARTADOS` en la misma empresa y sucursal del pedido.
- El avance del surtido no se guarda en `Pedido`; se calcula desde `PickingDetalle`.

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
  "almacen_destino": 10,
  "almacen_destino_nombre": "APARTADOS",
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

**Notas para Next.js**

- Sí es necesario enviar `picking_detalle` en el onboarding `POST`.
- El frontend decide qué tallas y cantidades se surtirán en ese picking; debe **respetar** `maximo_picking_permitido` reportado por el GET onboarding (el backend lo validará de nuevo).
- `almacen_destino` es opcional; si no se envía el backend resuelve `APARTADOS` y lo guarda.
- Los checkbox `generar_orden_*` pueden mandarse pero se ignoran en el POST de picking; la generación de OT se hace desde endpoints Produccion (ver sección `Orden de Bordado Onboarding`).
- El `Pedido` es solo referencia comercial; el avance real se consulta desde el historial de `PickingDetalle`.
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
El frontend solo selecciona la impresora, envía el ZPL vía Zebra Browser Print y notifica el resultado.

**Flujo recomendado**

1. `GET /buscar` (input tipo QA: SKU / nombre / código / cod_proscai) → selecciona SKU/producto.
2. `GET /preview` (con el `variante_id` o `producto_id` del paso 1) → trae datos del producto + ZPL + arreglo de `etiquetas` con su EPC.
3. El frontend itera `etiquetas` y envía a la impresora un ZPL por etiqueta (puedes usar `zpl_normal`, `zpl_rfid_first` como ejemplo o reconstruir el ZPL RFID usando el EPC de cada renglón). Usa `BrowserPrint-3.1.250.min.js` disponible en `/QA/browserprint/BrowserPrint-3.1.250.min.js/`.
4. Cuando termina la impresión, el frontend manda `POST /registrar-impresion/` para dejar trazabilidad → ahora **sí** aparecerá en `GET /api/v1/wms/etiquetas-rfid/`.

---

### 0) Buscador de SKU / Producto (mismo criterio que QA/imprimir_etiqueta)

- **Endpoint**: `GET /api/v1/wms/etiquetas-rfid/buscar/`
- **Descripción**: buscador simple y ligero para que el WMS muestre el mismo selector de QA: escribe texto, sugiere resultados, usuario selecciona 1 → alimenta el `GET /preview`.
- **Autenticación**: sesión/Token del usuario; respeta empresa (filtro por `empresa=user.empresa`) y scope multi-tenant.
- **Basado en**: la misma lógica Q de `QA/views.py:imprimir_etiqueta_workspace()` con OR sobre sku/nombre/producto/código/cod_proscai.

**Query params**

- `q`: texto libre. Ej: `93EO`, `playera`, `1000`, `AMB-1000`.
- Si `q` está vacío, devuelve primeras 60 entradas (30 variantes + 30 productos sin variantes) para scope empresa del usuario.

**Ejemplos**

- `GET /api/v1/wms/etiquetas-rfid/buscar/?q=RAYAS+THAI`
- `GET /api/v1/wms/etiquetas-rfid/buscar/?q=10005032XC`
- `GET /api/v1/wms/etiquetas-rfid/buscar/?q=`

**Respuesta shape**

```json
{
  "q": "10005032XC",
  "sucursal_ids": null,
  "resultados": [
    {
      "tipo": "variante",
      "id": 155,
      "producto_variante_id": 155,
      "producto_id": 91,
      "label": "10005032XC - CAMISA MANGA LARGA RAYAS THAI PREMIUM · VINO · 2XC",
      "sku": "10005032XC",
      "nombre": "CAMISA MANGA LARGA RAYAS THAI PREMIUM",
      "color_nombre": "VINO",
      "talla_nombre": "2XC",
      "codigo": "1000",
      "cod_proscai": null
    },
    {
      "tipo": "producto",
      "id": 211,
      "producto_variante_id": null,
      "producto_id": 211,
      "label": "1050 - HILO 100% POLIESTER",
      "sku": null,
      "nombre": "HILO 100% POLIESTER",
      "color_nombre": null,
      "talla_nombre": null,
      "codigo": "1050",
      "cod_proscai": null
    }
  ]
}
```

**Reglas para el frontend**

- El campo `label` es el texto que se pinta en la lista de resultados (igual que la lista QA).
- Si `tipo === "variante"`: pasa `producto_variante_id` al `GET /preview/?variante=`.
- Si `tipo === "producto"`: pasa `producto_id` al `GET /preview/?producto=`.
- `sucursal_ids` es `null` para admin/superuser, y un arreglo de IDs para operadores normales (por si el frontend quiere filtrar impresiones de solo sus sucursales).

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

### 5) Mensaje corto para Next.js

> Flujo tipo QA, pero oficial y con trazabilidad:
>
> 1. **Buscador** → `GET /api/v1/wms/etiquetas-rfid/buscar/?q=texto` → pintas la lista de resultados usando el campo `label` (misma UX que lista QA).
> 2. **Preview** → al seleccionar un renglón, si es `tipo=variante` llamas `GET /preview/?variante=producto_variante_id&cantidad=N`; si es `tipo=producto` llamas `GET /preview/?producto=producto_id&cantidad=N`. Obtienes `preview_data`, `zpl_rfid_first` y `etiquetas[]` con cada EPC.
> 3. **Imprimir** → por cada renglón en `etiquetas[]`, armas un ZPL igual que `zpl_rfid_first` pero reemplazando el EPC por el de la etiqueta actual, y se lo mandas a la Zebra vía Browser Print (usa la misma librería de QA: `/QA/browserprint/BrowserPrint-3.1.250.min.js/`).
> 4. **Registrar** → al terminar manda `POST /registrar-impresion` con `status=EXITO|FALLIDO`, nombre de impresora, y opcionalmente el arreglo `etiquetas[]` con los EPCs reales que salieron.
> 5. Ahora **sí** se verán resultados en `GET /api/v1/wms/etiquetas-rfid/` (la lista que hoy está vacía en `lazzar-erp.vercel.app/wms/rfid-labels`).

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

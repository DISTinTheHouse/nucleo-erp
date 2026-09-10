# 🏗️ Arquitectura y Blindaje del Sistema

Este documento describe la arquitectura técnica y las medidas de seguridad ("Blindaje") implementadas para operar como un backend robusto basado puramente en APIs.

## 1. Stack Tecnológico

- **Framework**: Django 6.0.2 (Python 3.12)
- **API**: Django REST Framework (DRF) 3.15+
- **Base de Datos**: PostgreSQL
- **Seguridad**: `django-axes`, `cryptography`, `corsheaders`

## 2. Estrategia de Blindaje (Security Hardening)

El sistema ha sido diseñado para "no confiar en el cliente" y validar todo en el servidor.

### A. Aislamiento Multi-tenant (Nivel DB)

Aunque es una base de datos compartida, el aislamiento lógico es absoluto:

- **DRF ViewSets (APIs operativas)**: Sobrescribimos `get_queryset()` en vistas multi-empresa para filtrar por la empresa del usuario.
- **Lógica**: `queryset.filter(empresa=request.user.empresa)` (o filtro equivalente cuando la empresa está en la relación, ej. `cotizacion__empresa`).
- **Resultado**: Un usuario no puede leer datos de otra empresa aunque manipule IDs en la URL.
- **Comportamiento esperado**:
  - Listados: `200 OK` con `[]` si el usuario no tiene empresa o no hay registros de su empresa.
  - Detalle: `404 Not Found` si el recurso existe pero pertenece a otra empresa.

#### A.1 Jerarquía de bypass autorizada (por ViewSet)

El sistema reconoce **3 niveles de usuario** respecto al aislamiento multi-empresa.
El filtro por `empresa` SÓLO aplica al nivel **Usuario normal** (nivel 3);
los niveles 1 y 2 **bypassean** todo scoping empresa y pueden operar sobre cualquier empresa del sistema.

Patrón canónico confirmado en: `catalogo/api/views.py:48`, `nucleo/permisos.py:101`, `compras/services/orden_compra_view_service.py:60-63`.

##### Niveles de acceso

| Nivel | Rol                   | ¿Cuándo aplica?                                     |
| :---: | --------------------- | --------------------------------------------------- |
| 🟢 1  | **Superusuario**      | `is_superuser=True` (campo Django core)             |
| 🟢 2  | **Admin. de Empresa** | `is_admin_empresa=True` (campo custom en `Usuario`) |
| 🔴 3  | **Usuario normal**    | Ambos flags anteriores = `False`                    |

##### Reglas de lectura (GET / `get_queryset`)

| Nivel | Comportamiento                                                                                                                                                                     |
| :---: | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1 / 2 | Devuelve **TODO** el QuerySet sin filtrar por empresa.                                                                                                                             |
|   3   | Filtro estricto: `qs.filter(empresa=request.user.empresa)` (o lookup anidado equivalente).<br>Si el usuario no tiene empresa asignada → `qs.none()` (respuesta `200 OK` con `[]`). |

##### Reglas de escritura (POST / PUT / PATCH / DELETE)

| Nivel | `perform_create` (POST)                                                                                                                                                                                                                                                   | `perform_update` / `perform_destroy`                                                                                                              |
| :---: | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1 / 2 | **Debe** enviar `empresa` explícitamente en el body (`validated_data`).<br>Si falta → `ValidationError`.                                                                                                                                                                  | Puede modificar/eliminar registros de **cualquier** empresa (sin restricción).                                                                    |
|   3   | Empresa tomada de `user.empresa` (**obligatorio**, si no → `PermissionDenied`).<br>Si envía una `empresa` distinta a la suya → `ValidationError`.<br>Se valida además que **toda FK cruzada** (cliente/proveedor/banco/cuenta/centro_costo/etc.) pertenezca a su empresa. | Permitido SÓLO si `instance.empresa_id == user.empresa.pk`.<br>Si pertenece a otra empresa → `PermissionDenied` (equivalente a `404` en detalle). |

#### A.2 Módulo Finanzas y Contabilidad — Blindaje multi-tenant canónico (19 ViewSets)

El módulo `/api/v1/finanzas/*` unifica el blindaje multi-tenant a través de 5 helpers reutilizables declarados en `finanzas/api/views.py:L75-156` que reemplazan todo chequeo inline inconsistente:

| Helper                                                          | Firma               | Responsabilidad                                                                                                                  |
| --------------------------------------------------------------- | ------------------- | -------------------------------------------------------------------------------------------------------------------------------- |
| `_puede_ver_todo(user)`                                         | `→ bool`            | Bypass `is_superuser or is_admin_empresa`. **Usar SIEMPRE** en lugar de `if getattr(user,"is_superuser", False)`.                |
| `_aplicar_scope_empresa(qs, user, lookup="empresa")`            | `→ QuerySet`        | GET: si puede_ver_todo devuelve `qs`; si no → `qs.filter(**{lookup: user.empresa})` o `.none()` si user sin empresa.             |
| `_resolve_empresa(user, validated_data, required=True)`         | `→ Empresa`         | WRITE: super/admin exige `empresa` en body (si falta → `ValidationError`); usuario normal → `user.empresa` o `PermissionDenied`. |
| `_validate_fk_empresa(user, related_obj, field_name, user_emp)` | `→ None`            | WRITE: Si no bypass → `related_obj.empresa_id != user_emp.pk` → `ValidationError({field_name})`. Super/admin skipea.             |
| `_user_empresa_or_none(user)`                                   | `→ Empresa \| None` | Helper safe para `user.empresa`, `None` si bypass.                                                                               |

**Lookups anidados seguros** (cuando el modelo NO tiene `empresa` FK directa):

- `ConciliacionBancaria` → `cuenta_bancaria__empresa`.
- `NotaCredito` → `factura__empresa`.
- `CuentaPorCobrar` (históricos pre-migración 0009) → `Q(empresa=e)|Q(empresa__isnull=True, factura__empresa=e)`.

**Endpoints agregados** (Dashboard / Generar alertas mora):

- `GET /api/v1/finanzas/dashboard-financiero/` → Super/admin acepta query param `?empresa_id=X` para filtrar por una empresa concreta; si no envía → requiere `user.empresa` o pide explícito (`ValidationError`).
- `POST /api/v1/finanzas/alertas-mora/generar/` → Super/admin envía `empresa_id` por body o query param; si lo omite → genera alertas para **TODAS** las empresas del sistema (param `None` a `AlertaMoraService`).

**Persistencia / Migraciones relacionadas al hardening**:

- `finanzas/migrations/0009_cuentaporcobrar_empresa_poliza_folio_consecutivo.py`
  - Añade `CuentaPorCobrar.empresa` FK (requerido por `_resolve_empresa` para scoping directo).
  - Restauración de `Poliza.folio_consecutivo` (campo de numeración de pólizas).

**Stack servicios reutilizables Finanzas** (`finanzas/services/*`):

- `CobroService`, `PagoService`, `ConciliacionService`, `MovimientoBancarioService`, `NotaCreditoService`, `AlertaMoraService`, `DashboardFinancieroService`, `PolizaService`.
- Management command: `python manage.py generar_alertas_mora` (batch nocturno multi-empresa).

### B. Validación de Datos (Nivel Serializer)

No permitimos basura en la BD.

- **RFCs**: Validación estricta de formato y checksum (algoritmo oficial SAT).
- **Archivos CSD**: Al subir sellos digitales (`.cer`, `.key`), se validan criptográficamente en memoria antes de guardarse. Si la contraseña no abre la llave o el RFC no coincide, se rechaza.

### C. Protección de Red y Transporte

- **CORS Estricto**: Solo se permiten peticiones desde el Frontend autorizado (configurado en `.env`).
- **Allowed Hosts**: El servidor rechaza peticiones con Host headers desconocidos.
- **Rate Limiting**: Protección contra ataques de fuerza bruta en el login (bloqueo temporal de IP).

### E. Integración con Google Drive (OAuth 2.0)

- **Modelo de seguridad**: una sola app OAuth (Client ID/Secret) para el sistema; cada usuario enlaza su cuenta con consentimiento propio.
- **Tokens por usuario**: se almacenan por usuario en `ia_cloud_integrations` (`access_token`, `refresh_token`, expiración y metadata).
- **Scopes**: lectura de Drive + email (`drive.readonly` y `userinfo.email`).
- **Regla de producto**: el Core aplica “una nube por usuario” (si el usuario ya conectó una, bloquea el onboarding de otras).
- **Transporte**: en producción se fuerza HTTPS y se confía en `X-Forwarded-Proto` para URLs correctas en entornos con proxy (Vercel).

### D. Doble Autenticación (2FA) en Login Web (Core)

- **Alcance**: Solo aplica a las vistas web del Core (login HTML), no a los endpoints API.
- **Control por usuario**: `Usuario.two_factor_enabled`.
- **Canales**:
  - **Email (SMTP)** usando Django nativo (`EmailMultiAlternatives`) con HTML + texto plano.
  - **SMS (Twilio, opcional)** vía llamada HTTP directa al API de Twilio.
- **Notas de seguridad**:
  - El código es temporal (TTL) y se valida contra hash en sesión.
  - Integrado con protección anti-fuerza-bruta (`django-axes`). En este proyecto el campo del POST es `username` (aunque el valor sea correo).

## 3. Flujo de Trabajo API-First

El backend actúa como una "Caja Negra" segura para el Frontend (Next.js).

1.  **Petición**: El frontend envía JSON + Token Bearer.
2.  **Gatekeeper**: Django verifica Token, IP (bloqueos) y Origen (CORS).
3.  **Contexto**: Se hidrata `request.user` y se determina su `empresa` activa.
4.  **Procesamiento**:
    - Se validan permisos (Role-Based Access Control) usando roles y claves de permiso (`R-XXX`, `E-XXX`, `D-XXX`).
    - Se ejecutan reglas de negocio (ej. validación SAT).
5.  **Respuesta**: JSON estructurado y códigos HTTP semánticos (200, 201, 400, 401, 403).

## 4. Estructura de Endpoints

- **`/api/v1/nucleo/`**: Gestión de estructura organizacional (Empresas, Sucursales).
- **`/api/v1/auth/`**: Gestión de sesión.
- **`/api/v1/sat/`**: Servicios fiscales y catálogos.
- **`/api/v1/finanzas/`**: Módulo Finanzas y Contabilidad multi-empresa (19 ViewSets). Scoping por `empresa=user.empresa`, bypass a `is_superuser` e `is_admin_empresa`. Incluye Clientes contables, Cuentas por Cobrar/Pagar, Facturas, Pólizas, Cuentas Contables, Centro de Costos, Bancos y Cuentas Bancarias, Cobros, Pagos, Movimientos Bancarios, Conciliaciones, Notas de Crédito, Alertas de Mora y Dashboard Financiero.
- **`/api/v1/inventarios/`**: Almacenes y Ubicaciones (scoping por Empresa/Sucursales; CRUD para admin_empresa/superuser).
- **`/api/v1/compras/`**: Flujo de órdenes de compra y recepciones.
- **`/api/v1/produccion/`**: Lista de materiales, órdenes de producción y procesos productivos.

### Endpoints Operativos Relevantes

- **Inventarios**:
  - `GET /api/v1/inventarios/existencias/`
  - `GET /api/v1/inventarios/movimientos/`
  - `GET /api/v1/inventarios/movimientos/{id}/detalles/`
  - `POST /api/v1/inventarios/operaciones/entrada`
  - `POST /api/v1/inventarios/operaciones/salida`
  - `POST /api/v1/inventarios/operaciones/ajuste`
- **Compras**:
  - `GET|POST /api/v1/compras/ordenes/onboarding/`
  - `GET|POST /api/v1/compras/recepciones/onboarding/`
- **Producción**:
  - `GET|POST /api/v1/produccion/orden-produccion/onboarding/`
  - `GET|POST /api/v1/produccion/lista-material/`
  - `PUT|PATCH /api/v1/produccion/lista-material/{bom_id}/`

## 5. Arquitectura Operativa por Módulo

### A. Inventarios

- **Fuente de verdad de stock**: el modelo `Existencia`.
- **Compatibilidad funcional**: `Existencia` soporta `producto` de forma directa y `producto_variante` como opcional.
- **Reglas operativas**:
  - `ENTRADA` suma cantidad.
  - `SALIDA` resta cantidad sin permitir valores negativos.
  - `AJUSTE` reemplaza la cantidad final.
- **Persistencia de movimientos**:
  - Se registra auditoría para consumo del frontend actual.
  - También se persiste en `MovimientoInventario` y `MovimientoInventarioDetalle` para trazabilidad formal.
- **Lectura de historial**:
  - El listado actual de movimientos sigue siendo compatible con el frontend.
  - El detalle del movimiento ya se expone por endpoint específico.

### B. Compras y Recepciones

- **Orden de compra**:
  - Se trabaja con onboarding simplificado.
  - El folio se asigna al aceptar la orden.
  - La orden de compra no afecta existencias.
- **Recepción**:
  - Es el evento que realmente incrementa inventario.
  - Soporta recepción parcial o total.
  - Centraliza la entrada de inventario para `OC` y `OP`.
  - Para `OC`, trabaja por `producto` tomado desde `OrdenCompraDetalle`.
  - Para `OP`, toma `producto` y `producto_variante` desde `OrdenProduccionDetalle`.
  - Usa series de folio de recepción como `RC`, `RT` o `RZ`.
- **Persistencia**:
  - La recepción actualiza `Existencia`.
  - Genera auditoría.
  - Genera también `MovimientoInventario` y `MovimientoInventarioDetalle`.
  - Cuando el origen es producción, persiste también la relación en `MovimientoInventario.op`.

### C. Producción

- **BOM / Lista de Materiales**:
  - Se administra desde `ListaMaterialBom` y `BomDetalle`.
  - El endpoint soporta lectura individual, consulta masiva (`bulk`) y edición.
- **Edición de BOM**:
  - `PUT/PATCH` sobre `lista-material/{bom_id}` actualiza encabezado.
  - Si se envía `materia_prima_detalle`, el backend reemplaza la composición actual por la nueva.
- **Orden de Producción**:
  - El frontend no necesita enviar `bom` en cada detalle.
  - El backend resuelve el BOM activo por `producto_variante` dentro de la empresa del usuario.
  - La entrada de producto terminado ya no requiere un flujo paralelo; se formaliza mediante `GET|POST /api/v1/compras/recepciones/onboarding/`.
  - La creación de OP consume inventario automáticamente usando la lista de materiales.
  - El contrato HTTP para frontend se mantiene en el mismo endpoint de onboarding.
- **Consumo de Producción**:
  - El backend crea `ConsumoProduccion` como encabezado de consumo.
  - El detalle de insumos consumidos se persiste en `consumo_detalle`.
  - También se registra la salida en `MovimientoInventario`, `MovimientoInventarioDetalle` y `AuditoriaEvento`.

## 6. Responsabilidades UI vs Backend

- **Django (Core)**: Servicios y reglas de negocio; HTML solo para vistas internas de lectura (administración técnica).
- **Next.js (Frontend)**: Toda la interacción de usuario final (CRUD de Inventarios, Usuarios, etc.) contra APIs DRF.
- **Permisos y Alcance**: Aplicados en el backend; el frontend no puede elevar privilegios ni salir de su empresa/sucursales.

## 6.1 Contrato Frontend-Backend

- **Frontend**:
  - Envía JSON simple y desacoplado de detalles internos de persistencia.
  - No necesita conocer si el backend guarda auditoría, movimientos formales o ambos.
  - No resuelve reglas de negocio como folios, BOM activo o validaciones de stock.
- **Backend**:
  - Resuelve folios y series.
  - Valida empresa, sucursal, almacén, cantidades y relaciones.
  - Persiste encabezados, detalles, auditoría y movimientos formales sin exponer complejidad adicional al cliente.

## 7. Auditoría

Cada escritura crítica genera un rastro:

- **Logs de API**: Tiempos de respuesta, usuario y status code.
- **Logs de Auditoría**: Cambios en modelos sensibles (quién cambió qué valor).
- **Movimientos de inventario**: además del log de auditoría, existen registros formales en tablas de movimientos para seguimiento operativo.

## 8. Sistema de Permisos (RBAC + Overrides)

El sistema implementa un control de acceso robusto y flexible:

1.  **Roles (Base)**: Se asignan roles a los usuarios (ej. "Ventas", "Almacén") que agrupan permisos.
2.  **Overrides (Excepciones)**: Se pueden otorgar o denegar permisos específicos a un usuario, sobrescribiendo sus roles.
    - **GRANT**: Otorga un permiso extra (ej. Usuario de "Ventas" que necesita ver "Inventarios").
    - **DENY**: Revoca un permiso crítico (ej. Usuario de "Admin" que no debe ver "Nómina").
3.  **Resolución**: La API de Login calcula los permisos efectivos (`(Roles + Grant) - Deny`) y los entrega al frontend. El frontend no necesita conocer la lógica compleja, solo recibe la lista final de permisos.

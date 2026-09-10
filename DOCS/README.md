# Documentación — nucleo-erp

Índice. Todo en español salvo donde se indique lo contrario.

## `api/` — contrato con el frontend
- [DOCUMENTACION_API.md](api/DOCUMENTACION_API.md) — referencia completa de endpoints, payloads y errores.
- [contratos-api-request-response.md](api/contratos-api-request-response.md) — auditoría de consistencia request/response (forma de errores, paginación, campos de estado).
- [integracion_nextjs_mesa_control_edicion_pedido.md](api/integracion_nextjs_mesa_control_edicion_pedido.md) — guía de integración de un flujo específico (edición de pedido en mesa de control).

## `arquitectura/` — cómo está construido
- [ARQUITECTURA_APP.md](arquitectura/ARQUITECTURA_APP.md) — stack, blindaje de seguridad, sesiones.
- [ESQUEMA_BD.md](arquitectura/ESQUEMA_BD.md) — modelos y relaciones.
- [dbdiagram.io.md](arquitectura/dbdiagram.io.md) — fuente DBML del esquema (para dbdiagram.io).
- [separacion-capas-frontend-backend-db.md](arquitectura/separacion-capas-frontend-backend-db.md) — auditoría de responsabilidades frontend/backend/DB.
- [flujo-comunicacion-nextjs-django-postgres.md](arquitectura/flujo-comunicacion-nextjs-django-postgres.md) — auth, CORS, conexión a Postgres, diagrama de componentes.
- [flujo-cotizacion-pedido-wms-inventario.md](arquitectura/flujo-cotizacion-pedido-wms-inventario.md) — flujo transaccional Cotización → Pedido → WMS → Inventario.

## `seguridad/`
- [CIBERSEGURIDAD.md](seguridad/CIBERSEGURIDAD.md), [SECURITY.md](seguridad/SECURITY.md) — postura de seguridad y reporte de vulnerabilidades.
- [identidad-rol-mesa-de-control.md](seguridad/identidad-rol-mesa-de-control.md) — diagnóstico de un bug de identidad de rol (histórico, ya corregido).

## `negocio/` — para usuarios y producto
- [GUIA_USUARIO.md](negocio/GUIA_USUARIO.md) — manual operativo.
- [ASISTENTE_IA.md](negocio/ASISTENTE_IA.md) — asistente de IA e integración con Google Drive.
- [bloqueo-lineas-hijas-finanzas.md](negocio/bloqueo-lineas-hijas-finanzas.md) — contexto de negocio del módulo de finanzas (histórico, ya corregido).

## `wms/` — notas internas de avance del almacén
- [wms_avances.md](wms/wms_avances.md), [PLAN_RFID.md](wms/PLAN_RFID.md).

## `varios/` — notas internas sin categoría fija
- [avances.md](varios/avances.md) — checklist general de fases del proyecto.
- [diagnostico-chrome-sesiones.md](varios/diagnostico-chrome-sesiones.md) — nota suelta, no es documentación del backend.

## `datos/` — no son documentación
Exports/artefactos de trabajo: catálogos de productos (`.xlsx`), un script de lectura (`leer_excel.py`) y un dump de escaneos RFID (`rfig-get.json`). Se quedan aquí por falta de mejor lugar, no por pertenecer al mapa de docs.

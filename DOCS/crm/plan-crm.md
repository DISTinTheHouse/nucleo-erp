# Plan del CRM (ventas)

Objetivo: embudo de ventas, seguimiento de clientes y cobranza con contexto. Lo usa solo ventas.

## Lo que ya existe
- `Prospecto` y `Oportunidad` en `ventas/models.py:17-37`, **vacíos** (solo la FK a empresa y a prospecto). Sin API.
- `Cotizacion.oportunidad`: FK ya lista (`ventas/models.py:76`).
- `Cliente.vendedores` (M2M) define quién ve cada cliente. El scope está en `terceros/scope.py` y `ventas/scope.py`.
- `Cotizacion` guarda el origen del cliente en booleanos (recompra, google, recomendación, etc.).
- Permisos `R-CRM`, `R-CRM-CLIENTES`, `R-CRM-COTIZACIONES`, `R-CRM-PEDIDOS` solo se usan en la búsqueda global.
- No hay: seguimiento, notas, tareas, recordatorios, scheduler ni Celery. `Notificacion` es una bandeja inmediata, sin fecha programada.
- `Cliente` no tiene `dias_credito` ni `limite_credito` (sí `Proveedor`).

## Modelo
Los modelos se quedan en `ventas` (la FK de `Cotizacion` ya apunta ahí). Código nuevo en archivos propios, no en el `views.py` de 3,500 líneas.

| Modelo | Campos clave |
|---|---|
| `Prospecto` | empresa, vendedor, nombre, contacto, teléfono, correo, origen, estatus (Nuevo, Contactado, Calificado, Descartado, Convertido), cliente (al convertir) |
| `Oportunidad` | empresa, vendedor, prospecto o cliente, título, etapa, monto estimado, fecha de cierre estimada, motivo de pérdida |
| `Actividad` | empresa, vendedor, tipo (Nota, Llamada, Correo, Visita, Tarea), asunto, detalle, fecha, fecha de vencimiento, completada, y FK a prospecto, oportunidad o cliente (al menos una) |
| `Cliente` (+campos) | `dias_credito`, `limite_credito` (los usa también la alerta de mora de finanzas) |

## Reglas
- Vendedor ve lo suyo; admin de empresa ve todo (mismo patrón que `alcance_cotizaciones`). Detalle de otra empresa o de otro vendedor: 404.
- Una oportunidad Ganada exige cliente. Convertir un prospecto crea el `Cliente` y lo asigna al vendedor.
- Una oportunidad Perdida exige motivo.
- Recordatorios: sin scheduler. Una tarea con `fecha_vencimiento` aparece en `GET /actividades/pendientes/` y el frontend la consulta. Se decide Celery solo si hace falta avisar sin que el usuario abra el sistema.

## Rendimiento
- Índices: (empresa, vendedor, etapa), (empresa, vendedor, completada, fecha_vencimiento) y los de búsqueda por texto.
- Embudo y KPIs: una consulta con agregados, no un conteo por etapa.
- Listados paginados con `select_related`.
- Ficha 360 del cliente: número fijo de consultas, con prueba `assertNumQueries`.

## Fases
- [ ] C0 Modelos, migración, scope y permisos `R-CRM-*`
- [ ] C0 `Cliente.dias_credito` y `limite_credito`
- [ ] C1 API de prospectos (CRUD, convertir a cliente)
- [ ] C1 API de oportunidades (CRUD, cambiar etapa, ganar, perder)
- [ ] C1 `GET /oportunidades/embudo/` (cantidad y monto por etapa)
- [ ] C1 Crear cotización desde una oportunidad
- [ ] C2 Actividades: notas, llamadas, tareas
- [ ] C2 `GET /actividades/pendientes/` (hoy, vencidas)
- [ ] C2 Notificar al vendedor cuando le asignan un prospecto
- [ ] C3 `GET /clientes/{id}/ficha-360/`: datos, vendedor, cotizaciones y pedidos recientes, actividad
- [ ] C3 Cobranza en la ficha: saldo, vencido a 30/60/90/120 días, cobros recientes
- [ ] C4 Prospecto y oportunidad en la búsqueda global
- [ ] C4 KPIs: conversión, monto por etapa, tareas vencidas

## Por definir con ventas
- [ ] Etapas del embudo (propuesta: Nueva, Contactada, Cotizada, Negociación, Ganada, Perdida)
- [ ] ¿Ganar una oportunidad crea el pedido o solo marca la etapa?
- [ ] ¿Correo o WhatsApp desde el CRM? Hoy no hay integración de WhatsApp; el correo existe solo vía Gmail en la app `ia`.

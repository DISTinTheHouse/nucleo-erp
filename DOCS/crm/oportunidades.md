# Oportunidades (CRM)

Lo que pasa antes de la cotización. El flujo cotización → mesa de control → pedido no cambia.

Base: `/api/v1/ventas/oportunidades/`

| Método | Ruta | Qué hace |
|---|---|---|
| GET | `/` | Lista. Filtros: `estado`, `cliente`, `vendedor`, `q`, `proximo_contacto_hasta=AAAA-MM-DD`. Con `?limit=` y `?offset=` pagina; sin `limit` regresa la lista completa. |
| POST | `/` | Crea. Empresa y vendedor los pone el servidor. |
| GET/PATCH | `/{id}/` | Detalle y edición. |
| DELETE | `/{id}/` | Baja lógica (`activo=false`). |
| POST | `/{id}/cotizar/` | Crea una cotización en Borrador ligada a la oportunidad. Responde `cotizacion_id`. |
| GET | `/embudo/` | Cantidad y monto por estado. |

## Estado
`etapa` la pone el vendedor: `Nueva`, `Contactada`, `Perdida`. `estado` se calcula:

1. `Perdida` si la etapa es Perdida.
2. `Ganada` si alguna cotización ligada está Autorizada.
3. `Cotizada` si tiene cotizaciones.
4. Si no, la etapa.

- Perdida exige `motivo_perdida`. Una oportunidad Ganada no se puede perder. No se cotiza una Perdida.
- Borrar una oportunidad (admin, ORM) no borra sus cotizaciones: la FK es `SET_NULL`.

## Quién ve qué
El vendedor ve las suyas. El admin de empresa ve todas las de su empresa. Otra empresa u otro vendedor: 404. El cliente debe ser de la misma empresa.

## Cliente
- `Cliente.dias_credito` y `Cliente.limite_credito`.
- `GET /terceros/clientes/{id}/` trae `resumen_comercial.cartera`: saldo por cobrar por moneda en `por_vencer`, `vencido_1_30`, `31_60`, `61_90`, `91_120` y `mas_120`.

## Rendimiento
El listado y el embudo hacen un número fijo de consultas (estado con dos `EXISTS`). Índices: (empresa, vendedor, etapa) y (empresa, vendedor, proximo_contacto).

## Seguimientos y recordatorios (nuevo; no cambia lo anterior)

Todo lo de arriba sigue igual. Lo siguiente es adicional.

El sistema **no avisa solo**: quien captura pone la fecha y el frontend consulta qué toca.

### Seguimientos: `/api/v1/ventas/oportunidad-seguimientos/`

Una nota, llamada, correo, visita o tarea de una oportunidad, con recordatorio opcional.

| Método | Ruta | Qué hace |
|---|---|---|
| POST | `/` | Crea. Manda `oportunidad`, `detalle`, y opcional `tipo` (`Nota`, `Llamada`, `Correo`, `Visita`, `Tarea`; por omisión `Nota`) y `fecha_recordatorio`. El vendedor lo pone el servidor. |
| GET | `/` | Lista. Filtros: `oportunidad`, `oportunidad_vendedor`, `tipo`, `pendientes=true`, `hasta=AAAA-MM-DD` o `hasta=hoy`. |
| PATCH | `/{id}/` | Edita `detalle`, `tipo`, `fecha_recordatorio` (reprogramar) o `completado`. |
| POST | `/{id}/completar/` | Lo marca completado. |
| DELETE | `/{id}/` | Lo borra. |

- **Qué toca hoy:** `GET /?pendientes=true&hasta=hoy`. Con `pendientes=true` salen solo los no completados, de oportunidades no perdidas, ordenados por `fecha_recordatorio` (los sin fecha al final). Con `hasta` solo los que tienen fecha igual o anterior (los vencidos incluidos).
- **Mis pendientes:** `GET /?pendientes=true&hasta=hoy&oportunidad_vendedor={id}`. `oportunidad_vendedor` deja solo los seguimientos de las oportunidades de ese vendedor (el dueño de la oportunidad, no quien escribió el seguimiento). Filtra sobre lo que el usuario ya puede ver, nunca lo amplía: un vendedor que pide el id de otro recibe una lista vacía. Si no es un número responde 400.
- Campos: `id`, `oportunidad`, `vendedor`, `tipo`, `detalle`, `fecha_recordatorio`, `completado`, `completado_at`, `created_at`.
- Campos de contexto, solo lectura, para pintar la fila sin pedir la oportunidad: `oportunidad_titulo`, `oportunidad_nombre_contacto`, `oportunidad_cliente_nombre` (`null` si la oportunidad no tiene cliente) y `vendedor_nombre` (nombre completo de quien escribió el seguimiento; cadena vacía si no tiene nombre capturado, `null` si el seguimiento no tiene autor). Salen igual que `cliente_nombre` y `vendedor_nombre` de la oportunidad, también en `onboarding.seguimientos`.
- Sin `?limit=` regresa la lista completa; con `?limit=` y `?offset=` pagina.
- No se puede cambiar un seguimiento a otra oportunidad.
- Visibilidad igual que las oportunidades: el vendedor ve los de las suyas, el admin los de su empresa; si no, 404.

### Transferir: `POST /oportunidades/{id}/transferir/`
Body `{"vendedor": id}`. Solo admin de empresa (otro usuario recibe 403). El vendedor debe ser de la misma empresa y estar activo. Quien la tenía deja de verla.

### Onboarding: `GET /oportunidades/{id}/onboarding/`
Toda la pantalla de una oportunidad en una llamada:

```json
{
  "oportunidad": { "...los mismos campos de siempre..." },
  "seguimientos": [ "...los 50 más recientes..." ],
  "pendientes": 2,
  "proximo_recordatorio": "2026-11-03",
  "cotizaciones": [{ "id": 12, "estatus": 3, "estatus_display": "AUTORIZADA", "created_at": "...", "autorizada_at": "...", "pedido_id": 7, "pedido_folio": "PED-001" }],
  "cartera": [{ "moneda": "MXN", "saldo_total": "0.00", "por_vencer": "0.00", "vencido_1_30": "0.00", "...": "..." }]
}
```

`cartera` sale vacía si la oportunidad no tiene cliente. `pedido_id` y `pedido_folio` son `null` hasta que mesa de control crea el pedido.

Rendimiento: el onboarding y los pendientes hacen un número fijo de consultas.

## Buscador global

Las oportunidades salen en `GET /api/v1/search/?q=` como el grupo `oportunidad` (al final de `grupos`).

- Se busca por subcadena en `titulo` y `nombre_contacto`, con 3 o más caracteres.
- Cada fila: `tipo`, `id`, `codigo` (siempre `null`), `titulo`, `subtitulo` (contacto o cliente) y `estatus` (el estado calculado).
- El grupo aparece con la clave `R-CRM-OPORTUNIDADES` o con `R-CRM`. El vendedor solo ve las suyas; el admin de empresa, las de su empresa.

# Oportunidades (CRM)

Lo que pasa antes de la cotización. El flujo cotización → mesa de control → pedido no cambia.

Base: `/api/v1/ventas/oportunidades/`

| Método | Ruta | Qué hace |
|---|---|---|
| GET | `/` | Lista. Filtros: `estado`, `cliente`, `vendedor`, `q`, `proximo_contacto_hasta=AAAA-MM-DD`. Con `?limit=` y `?offset=` pagina; sin `limit` regresa la lista completa. |
| POST | `/` | Crea. Empresa y vendedor los pone el servidor. |
| GET/PATCH | `/{id}/` | Detalle y edición. |
| DELETE | `/{id}/` | Baja lógica (`activo=false`). |
| POST | `/{id}/cotizar/` | Crea una cotización en Borrador ligada a la oportunidad. Responde `cotizacion_id`. Se puede llamar varias veces mientras siga abierta. |
| GET | `/{id}/cotizaciones/` | Cotizaciones `ligadas` y `disponibles` para ligar. |
| POST | `/{id}/cotizaciones/` | Liga cotizaciones que ya existen. |
| DELETE | `/{id}/cotizaciones/{cotizacion_id}/` | Quita una cotización de la oportunidad (no la borra). |
| GET | `/embudo/` | Cantidad y monto por estado. |

## Estado
`etapa` la pone el vendedor: `Nueva`, `Contactada`, `Perdida`. `estado` se calcula:

1. `Perdida` si la etapa es Perdida.
2. `Ganada` si alguna cotización ligada está Autorizada.
3. `Cotizada` si tiene cotizaciones.
4. Si no, la etapa.

- Perdida exige `motivo_perdida`. Una oportunidad Ganada no se puede perder.
- Ganada (el cliente aceptó) y Perdida están cerradas: `cotizar/` y ligar responden `400 {"etapa"}`; para vender más, otra oportunidad. Sin vendedor → `400 {"vendedor"}` (transferirla primero).
- Borrar una oportunidad (admin, ORM) no borra sus cotizaciones: la FK es `SET_NULL`.

## Varias cotizaciones por oportunidad

Una oportunidad tiene **N cotizaciones**: nuevas con `cotizar/` o ya hechas con `cotizaciones/`.

**`GET /{id}/cotizaciones/`**

```json
{
  "ligadas": [{ "id": 152, "estatus": 1, "estatus_display": "BORRADOR", "gran_total": "11600.00", "moneda": "MXN", "created_at": "...", "autorizada_at": null, "pedido_id": null, "pedido_folio": null }],
  "disponibles": [{ "id": 100, "...": "mismos campos" }]
}
```

- `moneda`: código ISO de la cotización (`MXN`, `USD`) o `null`. Viene también en `onboarding.cotizaciones`.
- `disponibles`: del mismo cliente y empresa, sin oportunidad y **sin autorizar**, que el usuario puede ver. Las 100 más recientes. Vacía si la oportunidad no tiene cliente.

**`POST /{id}/cotizaciones/`** con body `{"cotizaciones": [100, 99]}` → `200` con la misma forma del GET.

- Cada cotización debe ser visible para el usuario (el vendedor, las suyas; el admin, las de su empresa), del cliente de la oportunidad, sin autorizar y no estar en otra oportunidad. Si alguna no cumple, no se liga ninguna: `400 {"cotizaciones": "No se pueden ligar: [ids]. ..."}`.
- Repetir el POST con una ya ligada no falla.
- Oportunidad Ganada o Perdida → `400 {"etapa"}` (para vender más, otra oportunidad). Sin vendedor → `400 {"vendedor"}`. Sin cliente → `400 {"cliente"}`. Lista vacía, ids no numéricos o body que no es objeto → `400 {"cotizaciones"}`.
- Una autorizada no se liga: autorizar es lo que gana la oportunidad, y ya no se podría quitar.
- La cotización pasa al dueño de la oportunidad, también si liga un admin (igual que `transferir` y `cotizar`).
- Si otra persona liga una de esas cotizaciones al mismo tiempo, no se liga ninguna: `400 {"cotizaciones"}`; volver a pedir el GET.

**`DELETE /{id}/cotizaciones/{cotizacion_id}/`** → `204`. La cotización queda sin oportunidad y sigue siendo del dueño de la oportunidad (no regresa a su vendedor anterior).

- Autorizada → `400 {"cotizacion"}` (la oportunidad dejaría de estar Ganada).
- No ligada a esta oportunidad o no visible → `404`.

Integración rápida: botón "Ligar cotización" → GET, mostrar `disponibles` con checkbox → POST con los ids → pintar `ligadas` de la respuesta. En cada fila de `ligadas`, "Quitar" → DELETE y quitarla de la lista.

Rendimiento: el GET hace 3 consultas fijas y el POST 5, sin importar cuántas cotizaciones haya. En producción, desde fuera de Vercel: GET ~0.3–0.5 s y POST ~0.5–0.8 s, casi todo latencia de red a Supabase.

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
- Campos de contexto, solo lectura, para pintar la fila sin pedir la oportunidad: `oportunidad_titulo`, `oportunidad_nombre_contacto`, `oportunidad_cliente_nombre` (`null` si la oportunidad no tiene cliente o si un usuario que no es admin de empresa no puede ver a ese cliente, con la misma regla que `/terceros/clientes/`; el admin lo ve siempre, aunque el cliente esté dado de baja) y `vendedor_nombre` (nombre completo de quien escribió el seguimiento; cadena vacía si no tiene nombre capturado, `null` si el seguimiento no tiene autor). Salen también en `onboarding.seguimientos`. No amplían lo que acepta `?ordering=`.
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
  "cotizaciones": [{ "id": 12, "estatus": 3, "estatus_display": "AUTORIZADA", "gran_total": "11600.00", "moneda": "MXN", "created_at": "...", "autorizada_at": "...", "pedido_id": 7, "pedido_folio": "PED-001" }],
  "cartera": [{ "moneda": "MXN", "saldo_total": "0.00", "por_vencer": "0.00", "vencido_1_30": "0.00", "...": "..." }]
}
```

`cartera` sale vacía si la oportunidad no tiene cliente. `pedido_id` y `pedido_folio` son `null` hasta que mesa de control crea el pedido.

Rendimiento: el onboarding y los pendientes hacen un número fijo de consultas.

## Buscador global

Las oportunidades salen en `GET /api/v1/search/?q=` como el grupo `oportunidad` (al final de `grupos`).

- Se busca por subcadena en `titulo`, `nombre_contacto` y el `nombre` del cliente, con 3 o más caracteres. El nombre del cliente solo cuenta si el usuario puede ver a ese cliente (la misma regla que para mostrarlo).
- Cada fila: `tipo`, `id`, `codigo` (siempre `null`), `titulo`, `subtitulo` y `estatus` (el estado calculado).
- `titulo` es el nombre de la oportunidad con la misma regla que el resto de la app: el título; si no hay, el nombre del cliente (solo si el usuario puede ver a ese cliente: el superusuario y el admin de empresa siempre, aunque esté dado de baja; el resto, con la regla de `/terceros/clientes/`); si no, el contacto; y si no, `Oportunidad #<id>`. `subtitulo` es lo siguiente de esa cadena que no repita el título: con título propio, el cliente (si lo ve) o el contacto; con el cliente como título, el contacto; con el contacto o el respaldo como título, `null` (igual que en los demás grupos cuando no hay subtítulo). Se usa el `nombre` del cliente, no la razón social.
- El grupo aparece con la clave `R-CRM-OPORTUNIDADES` o con `R-CRM`. El vendedor solo ve las suyas; el admin de empresa, las de su empresa.

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

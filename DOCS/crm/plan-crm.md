# Plan del CRM (ventas)

El flujo cotización → mesa de control → pedido **no cambia**. El CRM es solo lo que pasa antes de la cotización:

```
Oportunidad → Cotización (ya existe) → Mesa de control → Pedido
```

## Qué se agrega
Una sola tabla nueva: `Oportunidad` (ya existe vacía en `ventas/models.py:28`, con la FK desde `Cotizacion.oportunidad` lista).

| Campo | Para qué |
|---|---|
| empresa, vendedor | Dueño. Vendedor ve lo suyo, admin de empresa ve todo. |
| cliente (opcional) | Si ya es cliente. Si no, se usan los datos de contacto. |
| nombre_contacto, telefono, correo | Prospecto sin crear otro modelo. |
| titulo, monto_estimado | Qué se vende y cuánto. |
| etapa | `Nueva`, `Contactada`, `Perdida` (las pone el vendedor). |
| motivo_perdida | Obligatorio al marcar Perdida. |
| proximo_contacto, notas | Seguimiento sin tabla de actividades. |

- **Cotizada y Ganada no se guardan, se calculan.** Cotizada = tiene cotizaciones ligadas. Ganada = alguna cotización está Autorizada. Así no se toca ningún paso del flujo actual.
- **Historial:** `HistoricalRecords` (ya usado en el repo) deja el rastro de cambios sin código extra.
- **No se crea:** Prospecto activo, Actividad, tareas, recordatorios programados, Celery ni integración de correo o WhatsApp.

## API (`/api/v1/ventas/oportunidades/`)
- CRUD con scope por empresa y vendedor (patrón de `alcance_cotizaciones`).
- `POST /{id}/cotizar/`: crea una cotización en Borrador con vendedor, cliente y oportunidad ya puestos. Desde ahí sigue el flujo de siempre.
- `GET /embudo/`: cantidad y monto por etapa en una sola consulta.
- `GET /?proximo_contacto_hasta=hoy`: lo que toca contactar hoy o ya vencido.

## Cliente
- `Cliente.dias_credito` y `limite_credito` (los usa también la alerta de mora de finanzas).
- `GET /clientes/{id}/resumen/`: cotizaciones y pedidos recientes y saldo vencido a 30/60/90/120 días. Ya existe `resumen_comercial` en `terceros/api/views.py:42`; se amplía, no se crea otro.

## Rendimiento
- Índice (empresa, vendedor, etapa) y (empresa, vendedor, proximo_contacto).
- Listados paginados con `select_related`; embudo con agregados.
- Prueba `assertNumQueries` en listado y resumen del cliente.

## Fases
- [ ] C0 Campos de `Oportunidad` + migración + scope + permisos `R-CRM-*`
- [ ] C0 `Cliente.dias_credito` y `limite_credito`
- [ ] C1 API de oportunidades (CRUD, perder con motivo)
- [ ] C1 `cotizar/` (crea cotización Borrador ligada)
- [ ] C1 `embudo/` y filtro de próximo contacto
- [ ] C2 Resumen del cliente con cobranza
- [ ] C2 Oportunidad en la búsqueda global

## Por definir con ventas
- [ ] ¿Les alcanzan las etapas Nueva, Contactada y Perdida (más las calculadas Cotizada y Ganada)?

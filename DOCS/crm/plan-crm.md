# CRM de ventas: estado

El flujo Cotización → Mesa de control → Pedido **no cambia**. El CRM es lo que pasa antes de la cotización.

Cómo se usa cada endpoint: `DOCS/crm/oportunidades.md`. Diagrama: `DOCS/arquitectura/flujo-crm-oportunidades.html`.

## Hecho
- [x] `Oportunidad`: contacto, monto, etapa, motivo de pérdida, próximo contacto, notas
- [x] Vendedor ve lo suyo, admin de empresa todo; otro: 404
- [x] CRUD, perder con motivo obligatorio
- [x] Estado calculado: Cotizada (tiene cotización) y Ganada (cotización autorizada)
- [x] `cotizar/`: crea la cotización en Borrador ligada
- [x] `embudo/`: cantidad y monto por estado
- [x] Filtro `proximo_contacto_hasta`
- [x] Seguimientos (nota, llamada, correo, visita, tarea) con fecha de recordatorio
- [x] Qué toca hoy: `pendientes=true&hasta=hoy`
- [x] `transferir/` a otro vendedor (solo admin)
- [x] `onboarding/`: toda la pantalla en una llamada
- [x] Cartera del cliente a 30/60/90/120 días y `dias_credito` / `limite_credito`
- [x] Oportunidades en el buscador global

## Pendiente
- [ ] Permisos `R-CRM-*` en los endpoints (hoy solo el scope; el frontend decide qué muestra)
- [ ] Convertir el contacto en `Cliente` desde la oportunidad (toca RFC y Facturama)
- [ ] Avisos automáticos de recordatorios (no hay scheduler: el frontend consulta)
- [ ] KPIs: conversión y tareas vencidas

## Por definir con ventas
- [ ] ¿Les alcanzan las etapas Nueva, Contactada y Perdida (más Cotizada y Ganada calculadas)?

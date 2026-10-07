# Trazabilidad del Pedido (folio P-…)

Con un folio `P-…`: **en qué paso va, cuánto lleva y si va a tiempo**.

- Una sola llamada: `GET /api/v1/ventas/pedidos/{id}/trazabilidad/` → contrato en [DOCUMENTACION_API.md › Trazabilidad del pedido](../api/DOCUMENTACION_API.md).
- Todo se calcula solo, con lo que ya registran producción, WMS y finanzas. Nadie mueve el estado a mano.
- Todo se mide en **piezas**.
- Código: [ventas/services/trazabilidad_service.py](../../ventas/services/trazabilidad_service.py).
- Diagrama: [trazabilidad-pedido.html](trazabilidad-pedido.html) (fuente editable: `trazabilidad-pedido.dataflow.json`).

---

## Pasos

```
Confirmado → Programado → Surtido → Maquila → Empacado → Embarcado
                                       └ Bordado · Reflejante · Corte manga · OP
Al lado: Facturado %  ·  Cobrado %
```

| Paso | Se mide con |
|---|---|
| Confirmado | Pedido autorizado + clasificación + fecha de confirmación |
| Programado | Piezas programadas por mesa de control |
| Surtido | Piezas con folio de picking |
| Maquila | Promedio de sus procesos: bordado y reflejante por avances; corte de manga por orden completada; OP por hitos de ruta crítica |
| Empacado | Piezas empacadas |
| Embarcado | Piezas empacadas con despacho |

- **Paso actual** = el último que ya arrancó.
- **Avance** = promedio de Surtido, Maquila, Empacado y Embarcado.

## Semáforo

Plazo: de `fecha_confirmacion` a la fecha compromiso de la clasificación.

| Color | Cuándo |
|---|---|
| 🔴 Rojo | Vencido, o avance 30+ puntos atrás del plazo consumido |
| 🟡 Amarillo | Avance 10+ puntos atrás, orden detenida, o piezas sin orden de trabajo con más del 25% del plazo consumido |
| 🟢 Verde | Va a tiempo |
| 🔵 Terminado | Embarcado al 100% |
| ⚪ Gris | Cancelado, sin clasificación o clasificación X |

Cada color trae sus `motivos` en texto.

---

## Plan

### Fase 0 — Decisiones ✅

- [x] Orden de pasos: Confirmado → Programado → Surtido → Maquila → Empacado → Embarcado
- [x] El plazo corre desde `fecha_confirmacion`
- [x] Umbrales: 10 / 30 puntos; 25% del plazo para piezas sin orden
- [x] El tracker termina en Embarcado
- [x] Surtido = tiene folio de picking
- [x] Serigrafía y cambio de talla fuera de v1 (no tienen orden de trabajo)
- [x] Pedido a `EN PROCESO` automático → se hace en Fase 3

### Fase 1 — Endpoint de detalle ✅

- [x] `GET /pedidos/{id}/trazabilidad/` (scope multi-tenant, 404 entre empresas)
- [x] Forma fija: 6 pasos y 4 procesos siempre, mismas llaves
- [x] Queries constantes (no crecen con órdenes ni renglones)
- [x] `cobrado_pct` solo para quien ve contabilidad
- [x] Fecha compromiso desde `fecha_confirmacion` (también cambia `fecha_entrega_min/max` del pedido)
- [x] 17 tests en `ventas/tests.py` (`PedidoTrazabilidadTests`)
- [x] Documentado en `DOCUMENTACION_API.md`

### Fase 2 — Tablero

- [ ] `GET /pedidos/trazabilidad/` paginado, mismo cálculo en bulk (`trazabilidad_pedidos()` ya existe)
- [ ] Filtros: semáforo, paso actual, clasificación, cliente, vendedor
- [ ] Orden: rojo → amarillo → verde, luego fecha compromiso
- [ ] Conteo por color para los chips
- [ ] Tests + medir con volumen real

### Fase 3 — Cerrar huecos de datos (con migraciones)

- [ ] Historial de estatus en órdenes de bordado, reflejante y corte de manga → hoy no se sabe cuándo cambió cada estatus
- [ ] Sellar `fecha_fin` al terminar una orden → hoy nadie la llena
- [ ] Avances de corte de manga → hoy solo cuenta la orden completada
- [ ] Fecha en `Despacho` → hoy no hay fecha real de embarque
- [ ] Pedido a `EN PROCESO` al primer picking u orden de trabajo → hoy nunca pasa de `AUTORIZADA`

### Fase 4 — Timeline y alertas

- [ ] Timeline en el detalle (requiere Fase 3)
- [ ] Notificación al vendedor y mesa de control cuando un pedido pasa a rojo u orden detenida
- [ ] Disparador periódico para alertas (Vercel Cron o GitHub Actions)

### Frontend (Next.js)

- [ ] Tracker horizontal: `pasos.map(...)`, color por `estado`
- [ ] Semáforo + `motivos` + fecha compromiso y días restantes
- [ ] Badges Facturado % / Cobrado %
- [ ] Maquila expandible → `procesos[].ordenes[]`, cada orden abre su detalle por `tipo` + `id`
- [ ] Tablero (Fase 2) con el estándar de listados

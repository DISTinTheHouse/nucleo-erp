# Flujo: Cotización → Pedido → WMS → Inventario

Repo: `nucleo-erp` (Django 6 + DRF). Apps: `ventas`, `wms`, `inventarios`.

## Resumen

```
Cotizacion (ventas)
  BORRADOR -> EN_REVISION -> AUTORIZADA
                                 |
                                 v
                        Pedido creado (estatus=AUTORIZADA)
                        + inventario SE DESCUENTA AQUÍ
                                 |
                                 v
                    Picking (wms) -- referencia pedido_id, sin validar estatus
                                 |
                                 v
                    Packing (wms) -- referencia picking_id
                                 |
                                 v
                    Despacho (wms) -- referencia packing_id, sin campo estatus
```

**Hallazgo clave**: WMS (picking/packing/despacho) nunca escribe en el ledger de inventario (`Existencia`/`MovimientoInventario`). El descuento de stock ocurre una sola vez, al autorizar la Cotización — antes de que exista ningún Picking. Picking/Packing/Despacho son papeleo de seguimiento sobre un stock que ya se descontó, no un disparador del ledger.

---

## 1. Máquina de estados por entidad

| Entidad | Estatus declarados | Estatus que el código realmente setea | Endpoint que lo hace |
|---|---|---|---|
| `Cotizacion` | `1 BORRADOR`, `2 EN_REVISION`, `3 AUTORIZADA`, `4 RECHAZADA`, `5 CAMBIOS_SOLICITADOS` | los 5 | ver tabla §2 |
| `Pedido` | `1 BORRADOR`, `2 POR_AUTORIZAR`, `3 AUTORIZADA`, `4 EN_PROCESO`, `5 CANCELADO` | **solo `3`**, hardcoded al crear | ninguno (`1,2,4,5` inalcanzables salvo `PATCH` genérico) |
| `Picking.estado` | `PENDIENTE, ASIGNADO, EN_PROCESO, PAUSADO, COMPLETADO, PARCIAL, CANCELADO` | **solo `PENDIENTE`** (default de creación) | ninguno — sin endpoint de update |
| `PickingDetalle.estado_linea` | `PENDIENTE, SURTIDA, PARCIAL, FALTANTE, CANCELADA` | **solo `PENDIENTE`** | ninguno |
| `Packing.estado` | `PENDIENTE, EN_PROCESO, COMPLETADO, CANCELADO` | **solo `PENDIENTE`** | ninguno |
| `Despacho` | *(no tiene campo estatus)* | — | completitud se infiere por existencia de filas `DespachoDetalle` |

`Pedido`, `Picking` y `Packing` declaran una máquina de estados completa en el modelo que el código nunca recorre. Ver §6.

---

## 2. Cotización → Pedido

Conversión: `_copiar_cotizacion_a_pedido()`, [ventas/api/views.py:1443-1583](../../ventas/api/views.py), llamada únicamente desde `autorizar()` ([:2028-2096](../../ventas/api/views.py)).

| Paso | Endpoint | Quién | Precondición | Efecto |
|---|---|---|---|---|
| Crear cotización | `POST /api/v1/ventas/cotizaciones/onboarding/` | vendedor | — | `estatus=1` |
| Enviar a revisión | `POST /cotizaciones/{id}/enviar-revision/` | vendedor/admin | `estatus in {1,4,5}` | `estatus=2`, notifica rol Mesa de Control |
| **Autorizar** | `POST /cotizaciones/{id}/autorizar/` | mesa control | `estatus==2`, sin Pedido activo previo | crea Pedido + descuenta inventario + `estatus=3` |
| Rechazar | `POST /cotizaciones/{id}/rechazar/` | mesa control | `estatus!=3` | `estatus=4` |
| Aceptar cambios | `POST /cotizaciones/{id}/aceptar-cambios/` | mesa control | `estatus==5`, Pedido ya existe | ajusta delta de inventario, reaplica campos al Pedido, `estatus=3` |
| Rechazar cambios | `POST /cotizaciones/{id}/rechazar-cambios/` | mesa control | `estatus==5` | revierte Cotizacion desde `aprobado_snapshot`, `estatus=3` |

`estatus=5 (CAMBIOS_SOLICITADOS)` solo se alcanza reeditando una cotización ya autorizada por el flujo de vendedor dentro de la ventana `COTIZACION_EDIT_WINDOW_MINUTES` — no hay una acción explícita de "mesa control pide cambios".

**Qué copia `_copiar_cotizacion_a_pedido`**:
- Encabezado 1:1 (moneda, tipo_pedido, condiciones de pago, envío, totales) — `Pedido.objects.create(...)`, hardcodea `estatus=3` en el mismo create.
- Defaults si la cotización los dejó vacíos: `forma_pago/metodo_pago/uso_cfdi` → `TRANSFERENCIA/PUE/G03`; `persona_pagos/correo_facturas/telefono_pagos` → datos del cliente, con fallback a un correo sintético `nombre@example.com` si no hay nada (frágil — [:1490-1493](../../ventas/api/views.py)).
- Folio: `_asignar_folio_pedido` (consecutivo de `SerieFolio`, tipo `PEDIDO`).
- Snapshot fiscal: `_snapshot_facturacion_pedido` copia razón social/RFC del cliente a columnas del Pedido.
- Líneas: `CotizacionDetalle → PedidoDetalle`, `CotizacionDetalleTalla → PedidoDetalleTalla`, `CotizacionServicioExtra → PedidoServicioExtra`, copia 1:1 incluyendo config de bordado/reflejante/corte_manga.

**El Pedido nace ya autorizado** (`estatus=3`) — no hay estado intermedio "por autorizar" real.

---

## 3. Pedido → WMS

WMS no valida `Pedido.estatus`. La única señal de "listo para picking" es que el Pedido exista (lo cual ya implica que la cotización fue autorizada).

### Picking

- Creación: `PickingService.handle_store()`, [wms/services/picking_service.py:254-346](../../wms/services/picking_service.py).
- Endpoints: `POST /api/v1/wms/pickings/` y `POST /api/v1/wms/pickings/onboarding/`.
- Contrato explícito en el docstring del servicio: crea `Picking` + `PickingDetalle` + folio. **No mueve inventario, no crea transferencias, no crea reservas.**
- `tipo` (ORDER/BATCH/WAVE/ZONE picking) se guarda pero no hay lógica que agrupe varios pedidos en un mismo picking — es siempre un pedido por picking.
- `cantidad_surtida` nunca se escribe en código no-test (solo un literal en `ventas/tests.py:1099`). Sin endpoint de update: no hay forma de marcar una línea como surtida ni de avanzar `Picking.estado`.

### Packing

- Creación desde un Picking: `PackingService.handle_store()`, [wms/services/packing_service.py:311-347](../../wms/services/packing_service.py).
- Endpoints: `POST /api/v1/wms/packings/` y `/onboarding/`.
- Bloquea el Picking (`select_for_update`), valida que no esté cancelado, valida cantidades solicitadas contra `PickingDetalle.cantidad_asignada` (no contra `cantidad_surtida`, que está muerto).
- Sin escritura a inventario. Sin endpoint de update.

### Despacho

- Creación desde un Packing: `DespachoService.handle_store()`, [wms/services/despacho_service.py:285-317](../../wms/services/despacho_service.py).
- Endpoints: `POST /api/v1/wms/despachos/` y `/onboarding/`.
- Bloquea el Packing, valida opcionalmente contra un `Envio` de logística.
- Sin campo `estatus` en el modelo — completitud = existencia de filas `DespachoDetalle`. Sin cancelación posible.
- Sin escritura a inventario.

### Fuera de este flujo (WMS)

- `Transferencia`: documento independiente de movimiento entre almacenes, con su propio enum de estatus y su propio servicio que sí escribe `MovimientoInventario` (`TransferenciaService`, invocado solo manualmente vía `POST /api/v1/wms/transferencias/`). No se dispara automáticamente desde picking/packing/despacho.
- `ReservaInventarioService` (`create_for_picking`/`apply_to_picking`) y el modelo `inventario_reservas`: código muerto — definidos, nunca invocados, sin endpoint que cree reservas.
- `ConteoCiclico`, `PickingOrdenTrabajo`, RFID: no participan en este flujo.

---

## 4. Dónde se descuenta el inventario

Todo ocurre en `ventas/api/views.py`, dentro de `CotizacionViewSet`, en el momento de autorizar — **no en WMS**.

- `_descontar_existencias_pedido()` ([:943-969](../../ventas/api/views.py)), llamada desde `autorizar()` ([:2063-2067](../../ventas/api/views.py)):
  1. `_build_pedido_inventory_plan()` suma cantidad requerida por (producto, variante) desde `PedidoDetalleTalla`.
  2. `_discount_existencias_pedido()` bloquea filas de `Existencia` (`select_for_update`, ordenadas por mayor cantidad), consume FIFO por saldo, lanza `ValidationError` si no alcanza.
  3. `_registrar_movimiento_inventario_pedido()` crea un `MovimientoInventario` (`tipo=SALIDA`, `pedido=pedido`) + `MovimientoInventarioDetalle` por línea.
  4. `_registrar_auditoria_inventario_pedido()` escribe `AuditoriaEvento` con el balance antes/después por ítem — se usa después para saber a qué almacén/ubicación restituir stock si la cotización se edita.
- `_ajustar_existencias_cambios_pedido()` ([:971-1023](../../ventas/api/views.py)), llamada desde `aceptar_cambios()`: calcula el delta entre el plan del Pedido existente y el de la Cotización editada, emite `ENTRADA` (restitución) y/o `SALIDA` según corresponda.

**No es** vía `OperacionInventarioViewSet` (`POST /api/v1/inventarios/operaciones/{entrada,salida,ajuste}/`) — ese endpoint es una vía manual separada, no la usa este flujo. `AjusteInventario` tampoco participa aquí; solo lo usa `OperacionInventarioViewSet` para el tipo `AJUSTE`.

---

## 5. Multi-tenant (`empresa`)

```
Cotizacion.empresa (nullable — inconsistente)
  -> Pedido.empresa (requerido, copiado al crear)
    -> Picking.empresa / Packing.empresa (requerido, copiado de pedido.empresa)
      -> Despacho (SIN FK empresa directa — se resuelve vía despacho.packing.empresa)
        -> MovimientoInventario.empresa (requerido, seteado explícito por cada writer)
```

`Existencia` tampoco tiene `empresa` directa — se resuelve vía `existencia.almacen.empresa`. La cadena es consistente (cada modelo la copia de su padre), pero `Despacho` y `Existencia` dependen de scoping por join en vez de columna propia, y `Cotizacion.empresa` nullable es una excepción al patrón del resto del proyecto.

---

## 6. Deuda técnica / hallazgos

1. **WMS no toca el ledger de inventario en este flujo.** El descuento es atómico y único, en `Cotizacion.autorizar()`, antes de que exista un Picking.
2. **Picking/Packing/Despacho no avanzan de estado.** `cantidad_surtida`, `Picking.estado`, `PickingDetalle.estado_linea`, `Packing.estado` nunca se escriben fuera del default de creación; no hay endpoint de update en `PickingViewSet`/`PackingViewSet`. El "% surtido" que describe el docstring de `PickingService` en realidad se calcula sobre `cantidad_asignada`, no sobre una cantidad físicamente surtida.
3. **`ReservaInventarioService` y `inventario_reservas` están muertos.** Diseño de reserva→aplicación-en-transferencia descrito en un comentario (`wms/services/existencia_service.py:26-32`) que no corresponde a lo que hace `PickingService.handle_store` (su propio docstring dice explícitamente que no crea reservas).
4. **`Despacho` no tiene campo estatus** — no se puede distinguir "en tránsito" de "entregado" ni cancelar un despacho.
5. **`Pedido.estatus` 2/4/5 son inalcanzables** por endpoint dedicado — nace en `3` y no hay ciclo de retroalimentación "WMS terminó → Pedido a EN_PROCESO/COMPLETADO".
6. **`Transferencia`** existe como documento independiente que sí mueve inventario, pero no está enlazada automáticamente al pipeline de picking pese a que `inventario_reservas` tiene FKs (`picking`, `transferencia`) que sugieren una integración planeada y nunca construida.
7. `# TODO: SAVE OP` literal en `ventas/api/views.py:1255` (acción `onboarding` de `CotizacionViewSet`).
8. Fallback frágil: correo sintético `nombre@example.com` al copiar Cotización → Pedido si no hay `correo_facturas` en ningún lado (`ventas/api/views.py:1490-1493`).

---

## 7. Endpoints — referencia rápida

| Recurso | Router | Acciones clave |
|---|---|---|
| Cotización | `/api/v1/ventas/cotizaciones/` | `onboarding`, `enviar-revision`, `autorizar`, `rechazar`, `aceptar-cambios`, `rechazar-cambios` |
| Pedido | `/api/v1/ventas/pedidos/` | CRUD genérico |
| Picking | `/api/v1/wms/pickings/` | `onboarding` (create); sin update |
| Packing | `/api/v1/wms/packings/` | `onboarding` (create); sin update |
| Despacho | `/api/v1/wms/despachos/` | `onboarding` (create); sin update, sin cancelar |
| Transferencia | `/api/v1/wms/transferencias/` | movimiento manual entre almacenes, sí escribe inventario |
| Operaciones inventario | `/api/v1/inventarios/operaciones/` | `entrada`, `salida`, `ajuste` — manual, no usado por este flujo |

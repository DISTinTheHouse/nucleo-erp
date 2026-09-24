# Flujo: Recepción → Calidad → Almacén (Compras)

Repo: `nucleo-erp` (Django 6 + DRF). Apps: `compras`, `inventarios`, `produccion` (OP), `QA`.

## Resumen — flujo ACTUAL

```
OrdenCompra (AUTORIZADA | PARCIALMENTE_RECIBIDA)
      ó  OrdenProduccion (PENDIENTE | PREPARACION | BORDANDO | REVISION)
                          |
                          v
        POST /api/v1/compras/recepciones/onboarding/
                          |
                          v
           Recepcion creada (folio serie RC/RT/RZ)
                          |
                          v
   _actualizar_existencias()  ->  Existencia += cantidad_recibida     [INMEDIATO]
                          |
                          v
   _crear_movimiento_formal_recepcion()  ->  MovimientoInventario(ENTRADA)  [INMEDIATO]
                          |
                          v
           Recepcion.estatus = RECIBIDA | PARCIAL
                          |
                          v
   OC  -> _actualizar_estatus_oc()  ->  RECIBIDA | PARCIALMENTE_RECIBIDA
   OP  -> si completa y cerrar_orden -> OrdenProduccion.estatus_op = COMPLETADO
```

**Hallazgo clave**: no existe paso de calidad. La mercancía queda disponible en `Existencia` (visible para picking/producción) en la MISMA transacción en que se registra la recepción, antes de que nadie la revise. El modelo ya tiene los campos para un gate de calidad — `Recepcion.EstatusRecepcion.EN_CALIDAD`/`CERRADA` y los modelos `CalidadInspeccion`/`CalidadInspeccionDetalle` — pero **nada en el código los usa**. No es que falte diseñarlo: falta conectarlo.

---

## 1. Máquina de estados — declarado vs. lo que el código realmente setea

| Entidad / campo | Valores declarados (`compras/models.py`) | Lo que el código realmente asigna | Dónde |
|---|---|---|---|
| `Recepcion.estatus` | `1 BORRADOR, 2 RECIBIDA, 3 PARCIAL, 4 EN_CALIDAD, 5 CERRADA, 6 CANCELADA` (:190-196) | `1` momentáneamente (ver nota), luego solo `2` o `3`. **`4 EN_CALIDAD` y `5 CERRADA` nunca se asignan en ningún endpoint.** `6 CANCELADA` tampoco. | `RecepcionViewSet.onboarding`, [`compras/api/views.py:1123`](../../compras/api/views.py) |
| `CalidadInspeccion.estado` | `pendiente, aprobada, rechazada, aprobada_condicion` (:397-407) | Ninguno — el modelo no tiene ViewSet, serializer, admin ni vista. Cero filas se crean por código de aplicación. | — |
| `CalidadInspeccionDetalle.resultado` | `cuarentena, liberado, concesion_cc, concesion lazzar, rechazo` (:419-432) | Ninguno, mismo motivo. | — |
| `EnvioProveedor.estado` | `preparando, en_transito, entregado, retrasado, cancelado` (:444-459) | Ninguno — mismo caso, código muerto. Fuera del alcance de este documento (no es Calidad), se anota porque comparte el patrón. | — |

Nota sobre `Recepcion.estatus = BORRADOR`: el constructor lo pone en `1` y hace un primer `.save()` (necesario para tener PK antes de crear `RecepcionDetalle`), pero un segundo `.save(update_fields=["estatus", ...])` lo pisa con `RECIBIDA`/`PARCIAL` antes de que la transacción de la request termine. Ningún `GET` externo puede observar `BORRADOR` en una recepción creada por este endpoint.

Búsqueda hecha para confirmar lo anterior (sin adivinar): `grep` de `EN_CALIDAD`, `CERRADA`, `CalidadInspeccion` en todo el repo — únicos resultados son la definición en `compras/models.py` y su migración (`0014_calidadinspeccion_calidadinspecciondetalle_and_more.py`). Cero resultados en `compras/admin.py`, cero en `QA/views.py` (1837 líneas, sí usa RFID/producción, no toca Calidad).

---

## 2. Sub-proceso: dos orígenes de Recepción (OC vs OP)

`RecepcionViewSet.onboarding` acepta *exactamente uno* de `orden_compra` / `orden_produccion` ([:1146](../../compras/api/views.py)) y bifurca la validación, pero converge en el mismo posteo a `Existencia`:

```
                    header.orden_compra XOR header.orden_produccion
                                   |
                 +-----------------+-----------------+
                 |                                   |
      origen = OC                          origen = OP
      oc.estatus in                        op.estatus_op in
      {AUTORIZADA,                         {PENDIENTE, PREPARACION,
       PARCIALMENTE_RECIBIDA}               BORDANDO, REVISION}
      [:1168]                              [:1186]
      valida contra                       valida contra
      OrdenCompraDetalle.cantidad         OrdenProduccionDetalle.cantidad
                 |                                   |
                 +-----------------+-----------------+
                                   |
                                   v
                    detalle_payload (mismo shape para ambos)
                                   |
                                   v
                _actualizar_existencias() [:751] (idéntico para OC/OP)
```

Diferencia relevante para el diseño de Calidad: en OP, `producto_variante` siempre viene poblado (el `RecepcionDetalle` trae variante); en OC, nunca. `CalidadInspeccionDetalle` no distingue el origen — apunta a `RecepcionDetalle`, así que hereda esa diferencia sin más trabajo.

---

## 3. Dónde se abona el inventario (hoy)

Todo ocurre dentro de `RecepcionViewSet.onboarding` ([`compras/api/views.py:1123`](../../compras/api/views.py)), en la misma transacción que crea la `Recepcion`:

1. `_actualizar_existencias()` ([:751-833](../../compras/api/views.py)) — por cada renglón: bloquea (`select_for_update`) o crea la fila de `Existencia`, incrementa `cantidad`/`stock`, crea el `RecepcionDetalle`. **Sin condición de calidad.**
2. `_crear_movimiento_formal_recepcion()` ([:834-865](../../compras/api/views.py)) — crea `MovimientoInventario` (`tipo_movimiento=ENTRADA`) + un `MovimientoInventarioDetalle` por renglón.
3. Se decide `Recepcion.estatus` (`RECIBIDA` si ya no queda pendiente, `PARCIAL` si sí) — línea [:1390](../../compras/api/views.py).
4. `_actualizar_estatus_oc()` ([:866-884](../../compras/api/views.py)) recalcula el estatus de la OC father, o si es OP y `cerrar_orden=True` y ya se completó, la OP pasa a `COMPLETADO` ([:1397-1399](../../compras/api/views.py)).

No hay punto de retorno entre "se registró la recepción" y "ya es stock disponible": son el mismo paso.

---

## 4. Flujo IDEAL (propuesto — nada de esto está implementado)

Construido **únicamente con los campos que ya existen y nadie usa** (no se inventó ningún campo nuevo, según lo pedido):

```
Recepcion (onboarding)
   -> registra RecepcionDetalle.cantidad_recibida (conteo físico)
   -> NO llama _actualizar_existencias ni _crear_movimiento_formal_recepcion
   -> estatus = EN_CALIDAD                      [hoy: RECIBIDA/PARCIAL y ya es stock]
                          |
                          v
        Calidad inspecciona cada RecepcionDetalle de esa Recepcion
        -> crea 1 CalidadInspeccion (header) + 1 CalidadInspeccionDetalle
           por RecepcionDetalle (cantidad_inspeccionada/aprobada/rechazada,
           resultado)
                          |
        +-----------------+------------------------+
        |                                          |
  resultado = liberado /                     resultado = rechazo /
  concesion_cc / concesion lazzar             cuarentena
  (por cantidad_aprobada)                            |
        |                                            v
        v                                   ??? SIN DEFINIR — ver §5
  AHORA SÍ: _actualizar_existencias()
  + _crear_movimiento_formal_recepcion()
  pero solo por cantidad_aprobada, no por
  cantidad_inspeccionada total
        |
        v
  Cuando TODO el detalle de la Recepcion ya
  tiene resultado de calidad -> Recepcion.estatus = CERRADA
  -> ahí (no antes) corre _actualizar_estatus_oc() / cierre de OP
```

Por qué esto calza con el modelo existente y no es una invención:
- `CalidadInspeccionDetalle` ya separa `cantidad_inspeccionada`, `cantidad_aprobada` y `cantidad_rechazada` como campos independientes — el modelo ya previó aceptación PARCIAL por renglón, no solo aprobar/rechazar todo el renglón.
- `resultado` ya tiene 5 valores, no 2 — alguien ya pensó en concesiones (`concesion_cc`, `concesion lazzar`) como una tercera vía entre "pasa" y "no pasa".
- El orden de los estatus de `Recepcion` (`BORRADOR, RECIBIDA, PARCIAL, EN_CALIDAD, CERRADA, CANCELADA`) ya deja `EN_CALIDAD` antes de `CERRADA` — coincide con "contado → en revisión → cerrado".

---

## 5. Abierto — no se define aquí a propósito

**Qué pasa con `cantidad_rechazada` (resultado `rechazo`/`cuarentena`) después de la inspección no está definido en el modelo ni se ha confirmado el proceso real de la maquila.** Opciones típicas (devolución a proveedor vía algo como `EnvioProveedor` invertido, baja/scrap, reintento de inspección desde `cuarentena`) no se eligen aquí — se preguntó y sigue sin respuesta. Sin esa regla no se puede construir la rama derecha del diagrama de §4, ni decidir si `cuarentena` necesita su propio sub-estado o basta con dejar el `CalidadInspeccionDetalle` sin resultado final.

---

## 6. Qué hay que construir para llegar del flujo actual al ideal

1. `CalidadInspeccion` / `CalidadInspeccionDetalle`: ViewSet + serializers + registro en `compras/api/urls.py`. Hoy no existe nada — ni admin.
2. `RecepcionViewSet.onboarding` ([:1123](../../compras/api/views.py)): quitar las llamadas a `_actualizar_existencias`/`_crear_movimiento_formal_recepcion` ([:1374-1375](../../compras/api/views.py)); dejar `estatus = EN_CALIDAD` en vez de `RECIBIDA`/`PARCIAL`.
3. Acción nueva (p. ej. `POST /api/v1/compras/calidad-inspecciones/onboarding/`) que, al crear el `CalidadInspeccionDetalle`, invoque `_actualizar_existencias`/`_crear_movimiento_formal_recepcion` **solo por `cantidad_aprobada`** — hoy esas dos funciones asumen que la cantidad recibida completa es la cantidad a abonar; hay que parametrizarlas por cantidad.
4. Mover `_actualizar_estatus_oc()` / el cierre automático de OP para que se disparen cuando Calidad libera, no cuando se recibe.
5. Resolver §5 antes de tocar la rama de rechazo.

## Impacto en lo que ya existe

- `RecepcionViewSet.handle_get_onboarding` ([:885](../../compras/api/views.py)) calcula "pendiente por recibir" = ordenado − recibido. Sigue sirviendo para eso; hace falta un cálculo nuevo y separado para "pendiente por liberar calidad" (recibido − aprobado).
- Todo lo que en el frontend asuma que `Recepcion.estatus in {RECIBIDA, PARCIAL}` significa "ya está en almacén" queda desactualizado: pasaría a significar "contado, pendiente de calidad".
- WMS/Picking (ver [`flujo-cotizacion-pedido-wms-inventario.md`](flujo-cotizacion-pedido-wms-inventario.md)) sigue asumiendo que lo que está en `Existencia` es disponible — eso no cambia. Solo cambia CUÁNDO algo llega a `Existencia`.

---

## 7. Endpoints — referencia rápida

| Recurso | Endpoint | Estado |
|---|---|---|
| Recepción | `POST/GET /api/v1/compras/recepciones/onboarding/` | Existe. Hoy posteа a `Existencia` de inmediato. |
| Calidad | — | **No existe.** `CalidadInspeccion`/`CalidadInspeccionDetalle` son solo modelo + migración. |
| Envío proveedor | — | No existe tampoco (mismo patrón, fuera de alcance de este doc). |

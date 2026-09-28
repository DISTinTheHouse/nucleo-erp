# Flujo: Recepción → Calidad → Almacén (Compras)

Repo: `nucleo-erp` (Django 6 + DRF). Apps: `compras`, `inventarios`, `produccion` (OP), `QA`, `hr`.

**Estado: implementado** (el gate de Calidad de la sección 1; NO incluye el encuadre RFID/scanner de OP ni la política de rechazo — ver §5 y §6).

## Resumen — flujo ACTUAL

```
OrdenCompra (AUTORIZADA | PARCIALMENTE_RECIBIDA)
      ó  OrdenProduccion (PENDIENTE | PREPARACION | BORDANDO | REVISION)
                          |
                          v
        POST /api/v1/compras/recepciones/onboarding/
                          |
                          v
   Recepcion creada (folio RC/RT/RZ) + RecepcionDetalle (conteo físico)
   NO toca Existencia. NO genera MovimientoInventario.
                          |
                          v
           Recepcion.estatus = EN_CALIDAD (4)   [siempre, sin excepción]
                          |
                          v
   OC  -> _actualizar_estatus_oc()  ->  RECIBIDA | PARCIALMENTE_RECIBIDA
   OP  -> si completa y cerrar_orden -> OrdenProduccion.estatus_op = COMPLETADO
   (basado en conteo físico, independiente de Calidad)
                          |
                          v
        POST /api/v1/compras/calidad-inspecciones/onboarding/
        (inspecciona TODO el detalle de la Recepcion en un solo envío)
                          |
        +-----------------+------------------------+
        |                                          |
  cantidad_aprobada > 0                    cantidad_rechazada > 0
        |                                          |
        v                                          v
  Existencia += cantidad_aprobada          NO entra a almacén
  MovimientoInventario(ENTRADA)            ??? sin definir — ver §5
        |
        v
  Recepcion.estatus = CERRADA (5)   [siempre, sea que se aprobó todo/nada/mixto]
```

**Antes de este cambio**, la recepción posteaba a `Existencia` de inmediato, en la misma transacción — cero paso de calidad. Ese hallazgo original (y por qué `EN_CALIDAD`/`CERRADA`/`CalidadInspeccion` ya existían pero no se usaban) se dejó intacto en §1 como referencia histórica.

---

## 1. Máquina de estados — cómo quedó

| Entidad / campo | Valores declarados (`compras/models.py`) | Quién los asigna ahora |
|---|---|---|
| `Recepcion.estatus` | `1 BORRADOR, 2 RECIBIDA, 3 PARCIAL, 4 EN_CALIDAD, 5 CERRADA, 6 CANCELADA` (:190-196) | `RecepcionViewSet.onboarding` pone `4 EN_CALIDAD` (línea 1508, antes ponía `2`/`3`). `CalidadInspeccionViewSet.onboarding` pone `5 CERRADA`. **`2 RECIBIDA` y `3 PARCIAL` quedaron muertos** — nadie los asigna ya; si tu frontend los busca, no los va a encontrar en recepciones nuevas. `6 CANCELADA` sigue sin usarse. |
| `CalidadInspeccion.estado` | `pendiente, aprobada, rechazada, aprobada_condicion` | `CalidadInspeccionViewSet._derivar_estado` (`compras/api/views.py:1633`): `aprobada` si todos los renglones son `liberado`/`concesion_cc`/`concesion lazzar`; `rechazada` si todos son `rechazo`/`cuarentena`; `aprobada_condicion` si es mixto. |
| `CalidadInspeccionDetalle.resultado` | `cuarentena, liberado, concesion_cc, concesion lazzar, rechazo` | Lo manda el cliente en el POST, por renglón. No hay validación cruzada contra `cantidad_aprobada`/`cantidad_rechazada` — es una etiqueta descriptiva, el split de cantidades lo controla el cliente. |
| `EnvioProveedor.estado` | `preparando, en_transito, entregado, retrasado, cancelado` | Ninguno — sigue sin usarse en ningún lado. Fuera de alcance de este trabajo. |

Nota que sigue aplicando: `Recepcion` nace en `BORRADOR` (`.save()` inicial, necesario para tener PK antes de crear `RecepcionDetalle`) y un segundo `.save()` lo pisa con `EN_CALIDAD` antes de que la transacción termine — ningún `GET` externo observa `BORRADOR`.

---

## 2. Sub-proceso: dos orígenes de Recepción (OC vs OP) — sin cambios

`RecepcionViewSet.onboarding` (`compras/api/views.py:1238`) sigue aceptando *exactamente uno* de `orden_compra`/`orden_produccion` (:1261) y bifurcando la validación de estatus (OC: `ESTATUS_OC_RECIBIBLES`, :1283 — OP: allowlist inline, :1298), pero converge en el mismo `detalle_payload`. Esto no cambió con el gate de Calidad — lo único que cambió es qué se hace con ese `detalle_payload` al final (ver §3).

Diferencia que Calidad hereda sin trabajo extra: en renglones de OP, `producto_variante` siempre viene poblado; en OC, nunca. `CalidadInspeccionDetalle` apunta a `RecepcionDetalle`, así que no necesita distinguir el origen.

---

## 3. Dónde se abona el inventario (ahora)

**Ya no es `RecepcionViewSet`.** Es exclusivo de `CalidadInspeccionViewSet` (`compras/api/views.py:1537`):

1. `RecepcionViewSet.onboarding` (:1238) crea `Recepcion` + llama `_crear_renglones_recepcion()` (:946-983) — SOLO crea `RecepcionDetalle` (conteo físico), sin tocar `Existencia`. Termina en `estatus=EN_CALIDAD` (:1508).
2. `_actualizar_estatus_oc()` (:984-1002) sigue corriendo aquí, sin esperar a Calidad — refleja "¿le falta algo por RECIBIR a la OC?", no "¿ya es stock?". Mismo criterio para el cierre automático de OP (`cerrar_orden`).
3. `POST /api/v1/compras/calidad-inspecciones/onboarding/` (:1703) — requiere inspeccionar **todo** el detalle de la recepción en un solo envío (sin inspección parcial por rondas). Por cada renglón con `cantidad_aprobada > 0`: `_abonar_renglon()` (:1564-1608, misma lógica que el `_actualizar_existencias` original, pero lee de un `RecepcionDetalle` ya existente en vez de crear uno) abona `Existencia`.
4. `_crear_movimiento_formal()` (:1609-1645) — crea `MovimientoInventario`(ENTRADA) + detalle, solo si algo se aprobó.
5. `Recepcion.estatus = CERRADA` (:1804) — siempre, al terminar el POST, sea que se aprobó todo, nada, o mixto (no hay estado intermedio "parcialmente cerrada").

Ahora SÍ hay punto de retorno entre "se contó" y "ya es stock disponible" — es exactamente el punto que faltaba.

---

## 4. Lo que ya calzaba con el modelo (razón por la que esto no fue una invención)

- `CalidadInspeccionDetalle` ya separaba `cantidad_inspeccionada`/`cantidad_aprobada`/`cantidad_rechazada` como campos independientes — el modelo ya preveía aceptación parcial por renglón.
- `resultado` ya tenía 5 valores, no 2 — alguien ya pensó en concesiones (`concesion_cc`, `concesion lazzar`) como tercera vía entre "pasa" y "no pasa".
- El orden de los estatus de `Recepcion` ya dejaba `EN_CALIDAD` antes de `CERRADA`.

Lo único que se agregó sin precedente en el modelo: `CalidadInspeccion.inspector` es un `hr.Empleado` obligatorio (`on_delete=PROTECT`, no nullable), y `Usuario`/`Empleado` **no están ligados en este sistema** (confirmado: cero FK entre ambos modelos). El endpoint no puede auto-resolver "el Empleado del usuario en sesión" — el cliente tiene que mandar `inspector` explícito, elegido de un catálogo (`GET .../calidad-inspecciones/onboarding/` lo trae). Es la gotcha más probable para el frontend.

---

## 5. Sigue abierto — a propósito, no se inventó una respuesta

**Qué pasa con `cantidad_rechazada` después de la inspección sigue sin definirse.** Hoy simplemente no entra a `Existencia` y ahí termina — no hay devolución a proveedor, no hay scrap, no hay reintento desde `cuarentena`. Si el negocio necesita rastrear eso, es trabajo aparte, sobre `EnvioProveedor` (invertido) o un modelo nuevo. Se preguntó dos veces en este proyecto y sigue sin respuesta — no bloqueó este cambio porque "no hacer nada con lo rechazado" es un default seguro (no inventa un flujo que nadie pidió), pero es deuda pendiente.

---

## 6. Lo que NO se construyó en este cambio (fuera de alcance, por decisión explícita de "no es para tanto")

- **Encuadre RFID/scanner para OP.** `RecepcionRFIDEncuadre`/`RecepcionRFIDLectura` (`QA/views.py`, `/QA/rfid/recepciones/`) siguen siendo SOLO para OC — `crear_encuadre`, `_build_recepcion_summary` y `_resolver_tag_recepcion` solo miran `orden_compra`, aunque el modelo ya tiene `op`/`tipo_origen`. Sigue siendo una isla aparte, desconectada de `Recepcion`/`CalidadInspeccion` (nadie escribe `RecepcionRFIDEncuadre.recepcion`).
- **Si el conteo por scanner debe ser obligatorio** (reemplazando la captura manual de `recepciones/onboarding/`) o quedarse como alternativa — sin decidir.
- **Almacén fijo para producto terminado** ("almacén 00"). Sigue sin ser una regla de negocio forzada por el backend — el almacén se elige libremente al capturar la Recepción, igual para OC y para OP, mientras pertenezca a la empresa/sucursal correcta.
- Cálculo de "pendiente por liberar calidad" (recibido − aprobado) como catálogo — hoy solo existe "pendiente por recibir" (ordenado − recibido) en `RecepcionViewSet.handle_get_onboarding` (:1003).

## Impacto en lo que ya existía

- **Rompe cualquier frontend que asumiera que `Recepcion.estatus in {RECIBIDA(2), PARCIAL(3)}` = "ya está en almacén".** Ahora esos dos valores no se asignan más; lo que hay es `EN_CALIDAD(4)` → `CERRADA(5)`.
- **Rompe cualquier frontend que leyera `movimiento_id`/`movimiento_inventario_id` de la respuesta de `recepciones/onboarding/`** — esos campos ya no vienen ahí, vienen (pueden venir `null`) en la respuesta de `calidad-inspecciones/onboarding/`.
- WMS/Picking (ver [`flujo-cotizacion-pedido-wms-inventario.md`](flujo-cotizacion-pedido-wms-inventario.md)) sigue asumiendo que lo que está en `Existencia` es disponible — eso no cambia. Solo cambia CUÁNDO algo llega a `Existencia`: ahora un ciclo después.

---

## 7. Endpoints — referencia rápida

| Recurso | Endpoint | Estado |
|---|---|---|
| Recepción | `POST/GET /api/v1/compras/recepciones/onboarding/` | Solo conteo físico. Ya NO toca `Existencia`. |
| Calidad | `POST/GET /api/v1/compras/calidad-inspecciones/onboarding/` | **Nuevo.** Único lugar que abona inventario. Inspección de todo el detalle en un solo envío, sin rondas. |
| Encuadre RFID (scanner) | `/QA/rfid/recepciones/` (HTML, no REST) | Sigue solo para OC. No conectado a `Recepcion`/`CalidadInspeccion`. |
| Envío proveedor | — | No existe (mismo patrón, fuera de alcance). |

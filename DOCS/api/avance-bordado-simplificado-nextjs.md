# Avance de bordado simplificado (sin talla)

El operario ya no elige talla/color/línea. Solo ve el folio de la OB y captura un total.

## Qué quitar de la pantalla
- El picker de talla/color/renglón.
- Deja solo: folio de OB + cantidad bordada (+ puntadas, si las capturan).

## Qué mandar — el backend ya lo soporta, no cambió nada

`POST /api/v1/produccion/bordado-avances/`

```json
{ "ob": 123, "cantidad_bordada": 50 }
```

Si también capturan puntadas:

```json
{ "ob": 123, "cantidad_bordada": 50, "puntadas_por_pieza": 8000, "puntadas_realizadas": 400000 }
```

**No mandes** `orden_bordado_detalle` ni `pedido_detalle_talla` — quítalos del payload, no hace falta nada más.

## Qué leer para mostrar el avance

`GET /api/v1/produccion/orden-bordado/{id}/` → `resumen_avance`:

- `cantidad_bordada_total`, `porcentaje_avance` — el total real de la OB completa.
- `puntadas_total`, `puntadas_porcentaje_avance` — igual, agregado.
- `por_detalle[]` — **ya no lo muestres**. Sigue viniendo en la respuesta (no se quitó), pero va a quedar siempre en 0 porque el avance ya no se registra por renglón/talla.

## Nota

Backend sin cambios: los campos de talla ya eran opcionales desde antes, solo se deja de enviarlos.

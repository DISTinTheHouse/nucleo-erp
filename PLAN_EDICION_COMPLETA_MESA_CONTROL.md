# Plan: Edición Completa del Pedido desde Mesa de Control

## Objetivo
Permitir que mesa de control pueda editar **TODO** el pedido incluyendo:
- ✅ Agregar/editar/eliminar líneas de productos
- ✅ Agregar/editar/eliminar tallas por línea
- ✅ Agregar/editar/eliminar servicios extras
- ✅ Editar productos de muestra (producto_nombre_externo)
- ✅ Sincronizar automáticamente con cotización

---

## Cambios Necesarios

### 1. **Modificar `_save_pedido_detalle()` en `ventas/utils/helpers.py`** (líneas 270-489)

**Problema Actual:**
- Líneas 477-489: Valida que NO haya "sobrantes" (renglones no tocados) y lanza error
- Líneas 465-475: Valida que NO haya tallas sobrantes y lanza error

**Cambio Necesario:**

```python
def _save_pedido_detalle(pedido_obj, rows, empresa, user):
    existing_detalles = {
        detalle.pk: detalle
        for detalle in PedidoDetalle.objects.filter(pedido=pedido_obj).prefetch_related("tallas")
    }
    touched_ids = set()
    
    # ... (todo el código de líneas 276-463 se mantiene igual) ...
    
    # ❌ ELIMINAR: Validación que bloquea eliminación de tallas sobrantes
    # Líneas 465-475: sobrantes = [row for row in tallas_actuales if row.pk not in tallas_usadas]
    # Este bloqueo se reemplaza por:
    
    # ✅ NUEVO: Permitir eliminación de tallas sobrantes
    sobrantes = [row for row in tallas_actuales if row.pk not in tallas_usadas]
    if sobrantes:
        # En lugar de lanzar error, eliminar las tallas sobrantes
        sobrantes_ids = [row.pk for row in sobrantes]
        PedidoDetalleTalla.objects.filter(pk__in=sobrantes_ids).delete()
    
    # ❌ ELIMINAR: Validación que bloquea eliminación de renglones completos
    # Líneas 477-489: Lanza error si hay sobrantes_detalle
    # Este bloqueo se reemplaza por:
    
    # ✅ NUEVO: Permitir eliminación de renglones sobrantes
    sobrantes_detalle = [
        detalle_id for detalle_id in existing_detalles.keys() 
        if detalle_id not in touched_ids
    ]
    if sobrantes_detalle:
        # En lugar de lanzar error, eliminar los renglones sobrantes
        PedidoDetalle.objects.filter(pk__in=sobrantes_detalle).delete()
```

---

### 2. **Modificar `_save_pedido_servicios_extras()` en `ventas/utils/helpers.py`** (líneas 492-511)

**Problema Actual:**
- Líneas 502-511: Valida que NO haya menos servicios y lanza error

**Cambio Necesario:**

```python
def _save_pedido_servicios_extras(pedido_obj, rows):
    existentes = list(PedidoServicioExtra.objects.filter(pedido=pedido_obj).order_by("id"))
    rows = rows or []
    
    # ✅ Crear/actualizar servicios enviados
    for index, row in enumerate(rows):
        extra = existentes[index] if index < len(existentes) else PedidoServicioExtra(pedido=pedido_obj)
        extra.nombre = row.get("nombre") or ""
        extra.monto = row.get("monto") or 0
        extra.cantidad = row.get("cantidad") or 1
        extra.visible_en_factura = bool(row.get("visible_en_factura", True))
        extra.save()
    
    # ✅ NUEVO: Permitir eliminación de servicios extras sobrantes
    if len(existentes) > len(rows):
        # Eliminar los servicios que ya no se envían
        ids_a_eliminar = [existentes[i].pk for i in range(len(rows), len(existentes))]
        PedidoServicioExtra.objects.filter(pk__in=ids_a_eliminar).delete()
    
    # ❌ ELIMINAR: La validación que lanzaba error (líneas 502-511)
```

---

### 3. **Modificar `_save_cotizacion_detalle()` en `ventas/utils/helpers.py`** (líneas 98-256)

**Problema Actual:**
- Línea 99: Elimina TODOS los detalles de cotización y recrea desde cero
- No hay lógica de "sobrantes"

**Actual (simplemente borra y recrea):**
```python
def _save_cotizacion_detalle(cotizacion_obj, rows, empresa, user):
    CotizacionDetalle.objects.filter(cotizacion=cotizacion_obj).delete()  # ← Delete all
    # ... recrea todos ...
```

**Ya está bien así** — la estrategia de "borrar todo y recrear" es segura para cotización porque:
1. La cotización ya está autorizada (estatus=3)
2. Se recrea con exactamente los mismos detalles que el pedido
3. No hay restricción de "no puedes eliminar si hay documentos ligados" (esos bloqueos están en Pedido, no en Cotización)

**Verificación:** El código ya funciona correctamente como está.

---

### 4. **Modificar `_save_servicios_extras()` (Cotización) en `ventas/utils/helpers.py`** (líneas 258-267)

**Situación Actual:**
```python
def _save_servicios_extras(cotizacion_obj, rows):
    CotizacionServicioExtra.objects.filter(cotizacion=cotizacion_obj).delete()  # ← Delete all
    for row in rows or []:
        CotizacionServicioExtra.objects.create(...)
```

**Ya está bien así** — misma razón que `_save_cotizacion_detalle()`.

---

### 5. **Actualizar flags en `PedidoViewSet._build_editar_mesa_control_contexto()`** (línea 2656)

**Cambio en `ventas/api/views.py` línea 2656-2658:**

```python
# ❌ ACTUAL:
"permite_eliminar_renglones": False,
"permite_eliminar_tallas": False,
"permite_eliminar_servicios_extras": False,

# ✅ NUEVO:
"permite_eliminar_renglones": True,
"permite_eliminar_tallas": True,
"permite_eliminar_servicios_extras": True,
```

---

## Descripción de Cambios Detallada

### Cambio 1: Permitir eliminación de tallas sobrantes

**Ubicación:** `ventas/utils/helpers.py`, líneas 465-475

**Antes:**
```python
sobrantes = [row for row in tallas_actuales if row.pk not in tallas_usadas]
if sobrantes:
    sobrantes_ids = ", ".join(str(row.pk) for row in sobrantes)
    raise ValidationError(
        {
            "detalle": (
                "No se puede quitar tallas existentes del pedido mientras "
                f"haya trazabilidad asociada. Tallas sobrantes: {sobrantes_ids}."
            )
        }
    )
```

**Después:**
```python
sobrantes = [row for row in tallas_actuales if row.pk not in tallas_usadas]
if sobrantes:
    # Permitir la eliminación: el usuario puede quitar tallas desde mesa de control
    # porque ya validamos en _get_bloqueos_edicion_estricta() que no hay documentos ligados
    sobrantes_ids = [row.pk for row in sobrantes]
    PedidoDetalleTalla.objects.filter(pk__in=sobrantes_ids).delete()
```

---

### Cambio 2: Permitir eliminación de renglones sobrantes

**Ubicación:** `ventas/utils/helpers.py`, líneas 477-489

**Antes:**
```python
sobrantes_detalle = [
    detalle_id for detalle_id in existing_detalles.keys() if detalle_id not in touched_ids
]
if sobrantes_detalle:
    raise ValidationError(
        {
            "detalle": (
                "No se pueden eliminar renglones existentes del pedido en edición "
                "estricta. Deben cancelarse primero los documentos ligados o "
                "conservar el renglón original en el payload."
            )
        }
    )
```

**Después:**
```python
sobrantes_detalle = [
    detalle_id for detalle_id in existing_detalles.keys() if detalle_id not in touched_ids
]
if sobrantes_detalle:
    # Permitir la eliminación: el usuario puede quitar renglones desde mesa de control
    # porque ya validamos en _get_bloqueos_edicion_estricta() que no hay documentos ligados
    PedidoDetalle.objects.filter(pk__in=sobrantes_detalle).delete()
```

---

### Cambio 3: Permitir eliminación de servicios extras sobrantes

**Ubicación:** `ventas/utils/helpers.py`, líneas 502-511

**Antes:**
```python
if len(existentes) > len(rows):
    raise ValidationError(
        {
            "servicios_extras": (
                "No se pueden eliminar servicios extras existentes desde la edición "
                "estricta. Manténgalos en el payload o regularice primero los "
                "documentos ligados."
            )
        }
    )
```

**Después:**
```python
if len(existentes) > len(rows):
    # Permitir la eliminación: solo mantener los servicios extras que vienen en el payload
    ids_a_eliminar = [existentes[i].pk for i in range(len(rows), len(existentes))]
    PedidoServicioExtra.objects.filter(pk__in=ids_a_eliminar).delete()
```

---

### Cambio 4: Actualizar flags de contexto

**Ubicación:** `ventas/api/views.py`, líneas 2656-2658

```python
# Dentro de _build_editar_mesa_control_contexto()
return {
    # ... otros campos ...
    "permite_eliminar_renglones": True,        # ← Cambiar de False a True
    "permite_eliminar_tallas": True,           # ← Cambiar de False a True
    "permite_eliminar_servicios_extras": True, # ← Cambiar de False a True
    # ... otros campos ...
}
```

---

## Restricciones y Seguridad

**Importante:** Los cambios respetan las restricciones de **bloqueos**. Mesa de control ya NO PUEDE editar si:
- Hay factura emitida
- Hay órdenes de producción activas
- Hay picking activo
- Hay reservas inventario activas
- etc.

La lógica de `_get_bloqueos_edicion_estricta()` **ya valida todo esto** antes de permitir el POST a `editar-mesa-control`. Por lo tanto, es seguro permitir eliminaciones porque:

1. Si hay documentos ligados → el endpoint retorna 409 CONFLICT (ni siquiera entra a `_save_pedido_detalle`)
2. Si NO hay documentos ligados → es seguro eliminar porque nada depende de esas líneas

---

## Flujo de Uso (desde Frontend)

### Caso 1: Eliminar una línea completa

```json
{
  "pedido": { ...encabezado... },
  "detalle": [
    {
      "id": 1,    // Línea existente
      "producto": 77,
      "tallas": [{ "talla": 4, "cantidad": 10, ... }]
    }
    // ← Línea con id=2 NO aparece en el payload → SE ELIMINA
  ],
  "servicios_extras": [...]
}
```

### Caso 2: Eliminar una talla de una línea

```json
{
  "detalle": [
    {
      "id": 1,
      "producto": 77,
      "tallas": [
        { "talla": 4, "cantidad": 10, ... }
        // ← Talla con id=5 no aparece → SE ELIMINA
      ]
    }
  ]
}
```

### Caso 3: Eliminar un servicio extra

```json
{
  "servicios_extras": [
    { "nombre": "Urgencia", "monto": "50.00", ... }
    // ← Servicio extra original que tenía 2 items, ahora solo 1 → EL SEGUNDO SE ELIMINA
  ]
}
```

---

## Sincronización Automática

Después de estos cambios, cuando mesa de control guarda:

```
POST /api/v1/ventas/pedidos/{id}/editar-mesa-control/
  ↓
1. Actualiza Pedido (líneas, tallas, servicios editados/nuevos/eliminados)
  ↓
2. Sincroniza Cotización (copia exactamente el mismo estado)
  ↓
3. Actualiza aprobado_snapshot (auditoría)
  ↓
200 OK:
{
  "pedido": {...actualizado, incluyendo eliminaciones},
  "cotizacion": {...sincronizado, incluyendo eliminaciones},
  "sincronizado": true
}
```

---

## Tests a Considerar

Después de implementar, agregar tests para:

```python
def test_mesa_control_puede_eliminar_linea_completa(self):
    # Crear pedido con 2 líneas
    # POST editar-mesa-control con solo 1 línea
    # Verificar que la segunda se eliminó

def test_mesa_control_puede_eliminar_talla_de_linea(self):
    # Crear línea con 2 tallas
    # POST editar-mesa-control con solo 1 talla
    # Verificar que la segunda se eliminó

def test_mesa_control_puede_eliminar_servicio_extra(self):
    # Crear pedido con 2 servicios extras
    # POST editar-mesa-control con solo 1 servicio
    # Verificar que el segundo se eliminó

def test_eliminacion_se_sincroniza_con_cotizacion(self):
    # Eliminar línea desde mesa de control
    # Verificar que la cotización también queda sin esa línea

def test_bloquea_eliminacion_si_hay_factura_ligada(self):
    # Crear factura ligada al pedido
    # POST editar-mesa-control para eliminar línea
    # Verificar que retorna 409 CONFLICT con bloqueos
```

---

## Resumen

| Cambio | Archivo | Líneas | Complejidad |
|--------|---------|--------|-------------|
| Permitir eliminar tallas | helpers.py | 465-475 | Baja |
| Permitir eliminar líneas | helpers.py | 477-489 | Baja |
| Permitir eliminar servicios | helpers.py | 502-511 | Baja |
| Actualizar flags | views.py | 2656-2658 | Trivial |

**Total:** 4 cambios pequeños, sin lógica compleja.

**Impacto:** Mesa de control obtiene edición **COMPLETA** del pedido, incluyendo eliminaciones.

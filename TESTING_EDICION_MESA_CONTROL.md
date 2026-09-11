# Testing: Edición Completa del Pedido desde Mesa de Control

## Preparación del Entorno

```bash
# 1. Asegurarse de que mesa de control tiene clave_departamento
UPDATE roles SET clave_departamento = 'MESACONTROL' WHERE codigo LIKE 'MESACONTROL%';

# 2. Ejecutar migraciones (si hay)
python manage.py migrate

# 3. Ejecutar tests
python manage.py test ventas.tests.PedidoViewSetScopeTenantTests -v 2
```

---

## Test Case 1: Eliminar una línea completa

### Preparación
```bash
# Crear un pedido con 2 líneas
POST /api/v1/ventas/pedidos/

Línea 1: Producto A, Talla M, cantidad 10
Línea 2: Producto B, Talla L, cantidad 5
```

### Test
```bash
# Obtener contexto
GET /api/v1/ventas/pedidos/{pedido_id}/editar-mesa-control-contexto/

# Verificar respuesta
{
  "editable": true,
  "permite_eliminar_renglones": true,  # ✅ NUEVO
  "bloqueos": []
}

# Editar pedido: enviar solo línea 1 (línea 2 se omite)
POST /api/v1/ventas/pedidos/{pedido_id}/editar-mesa-control/

{
  "pedido": {
    "sucursal": 1,
    "cliente": 15,
    "subtotal": "850.00",
    "gran_total": "986.00"
  },
  "detalle": [
    {
      "id": {detalle_1_id},
      "producto": {prod_a_id},
      "precio_unitario": "85.00",
      "tallas": [
        {
          "talla": 4,
          "cantidad": 10,
          "lleva_bordado": false
        }
      ]
    }
    // ← Línea 2 NO aparece
  ],
  "servicios_extras": []
}
```

### Verificación
```bash
# Respuesta debe ser 200 OK
✅ status_code == 200
✅ response.json()["sincronizado"] == true

# Pedido debe tener solo 1 línea
GET /api/v1/ventas/pedidos/{pedido_id}/

✅ pedido.detalles.count() == 1
✅ pedido.detalles[0].id == {detalle_1_id}
✅ pedido.detalles[0].tallas.count() == 1

# Cotización debe estar sincronizada
GET /api/v1/ventas/cotizaciones/{cotizacion_id}/

✅ cotizacion.cotizaciondetalle.count() == 1  # Sincronizado
✅ cotizacion.subtotal == "850.00"
✅ cotizacion.gran_total == "986.00"
```

---

## Test Case 2: Eliminar una talla de una línea

### Preparación
```bash
# Crear un pedido con 1 línea que tiene 2 tallas
Línea 1: Producto A
  - Talla M (id=200), cantidad 10
  - Talla L (id=201), cantidad 5
```

### Test
```bash
# Editar pedido: mantener solo Talla M
POST /api/v1/ventas/pedidos/{pedido_id}/editar-mesa-control/

{
  "pedido": {...},
  "detalle": [
    {
      "id": {detalle_1_id},
      "producto": {prod_a_id},
      "tallas": [
        {
          "talla": 4,  // M
          "cantidad": 10
        }
        // ← Talla L no aparece
      ]
    }
  ],
  "servicios_extras": []
}
```

### Verificación
```bash
✅ status_code == 200
✅ pedido.detalles[0].tallas.count() == 1
✅ pedido.detalles[0].tallas[0].talla_id == 4  # M
✅ cotizacion.cotizaciondetalle.first().tallas.count() == 1  # Sincronizado
```

---

## Test Case 3: Eliminar servicios extras

### Preparación
```bash
# Crear pedido con 2 servicios extras
1. Urgencia - $50.00
2. Programación - $100.00
```

### Test
```bash
# Editar pedido: mantener solo Urgencia
POST /api/v1/ventas/pedidos/{pedido_id}/editar-mesa-control/

{
  "pedido": {...},
  "detalle": [...],
  "servicios_extras": [
    {
      "nombre": "Urgencia",
      "monto": "50.00",
      "cantidad": 1,
      "visible_en_factura": true
    }
    // ← Programación no aparece
  ]
}
```

### Verificación
```bash
✅ status_code == 200
✅ pedido.servicios_extras.count() == 1
✅ pedido.servicios_extras[0].nombre == "Urgencia"
✅ cotizacion.servicios_extras.count() == 1  # Sincronizado
```

---

## Test Case 4: Agregar nueva línea

### Preparación
```bash
# Pedido con 1 línea existente
```

### Test
```bash
# Editar pedido: mantener línea 1, agregar línea 2
POST /api/v1/ventas/pedidos/{pedido_id}/editar-mesa-control/

{
  "pedido": {...},
  "detalle": [
    {
      "id": {detalle_1_id},  // Existente
      "producto": {prod_a_id},
      "tallas": [...]
    },
    {
      // ← Sin id: NUEVA línea
      "producto": {prod_b_id},
      "precio_unitario": "95.00",
      "tallas": [
        {
          "talla": 5,
          "cantidad": 10
        }
      ]
    }
  ],
  "servicios_extras": []
}
```

### Verificación
```bash
✅ status_code == 200
✅ pedido.detalles.count() == 2
✅ pedido.detalles[1].producto_id == {prod_b_id}  # Nueva
✅ cotizacion.cotizaciondetalle.count() == 2  # Sincronizado
```

---

## Test Case 5: Modificar configuración de bordado

### Preparación
```bash
# Pedido con talla sin bordado
Talla M: lleva_bordado = false
```

### Test
```bash
# Editar pedido: activar bordado en Talla M
POST /api/v1/ventas/pedidos/{pedido_id}/editar-mesa-control/

{
  "detalle": [
    {
      "id": {detalle_1_id},
      "tallas": [
        {
          "talla": 4,
          "cantidad": 10,
          "lleva_bordado": true,  # ← Activar
          "bordado_config": {
            "ubicaciones": [
              {
                "codigo": "PE"
              }
            ]
          }
        }
      ]
    }
  ]
}
```

### Verificación
```bash
✅ status_code == 200
✅ pedido_talla.lleva_bordado == true
✅ pedido_talla.bordado_config is not None
✅ cot_talla.lleva_bordado == true  # Sincronizado
```

---

## Test Case 6: Cambiar producto de muestra

### Preparación
```bash
# Línea con muestra
{
  "id": {detalle_id},
  "producto_nombre_externo": "Prototipo antiguo"
}
```

### Test
```bash
# Cambiar nombre de muestra
POST /api/v1/ventas/pedidos/{pedido_id}/editar-mesa-control/

{
  "detalle": [
    {
      "id": {detalle_id},
      "producto_nombre_externo": "Prototipo nuevo",
      "tallas": [...]
    }
  ]
}
```

### Verificación
```bash
✅ status_code == 200
✅ pedido_detalle.producto_nombre_externo == "Prototipo nuevo"
✅ cot_detalle.producto_nombre_externo == "Prototipo nuevo"  # Sincronizado
```

---

## Test Case 7: Verificar bloqueos aún funcionan

### Preparación
```bash
# Crear pedido con factura emitida
pedido = Pedido.objects.create(...)
factura = Factura.objects.create(pedido=pedido, estatus=EMITIDA)
```

### Test
```bash
# Intentar editar pedido (debe estar bloqueado)
POST /api/v1/ventas/pedidos/{pedido_id}/editar-mesa-control-contexto/

{
  "editable": false,
  "codigo": "pedido_con_bloqueos",
  "bloqueos": [
    {
      "tipo": "factura_emitida",
      "folio": "FAC-0001"
    }
  ]
}
```

### Verificación
```bash
✅ status_code == 200
✅ response["editable"] == false
✅ len(response["bloqueos"]) > 0

# Intentar guardar cambios (debe retornar 409)
POST /api/v1/ventas/pedidos/{pedido_id}/editar-mesa-control/

✅ status_code == 409
✅ response["editable"] == false
```

---

## Test Case 8: Combinación: Modificar, eliminar y agregar

### Preparación
```bash
# Pedido con 2 líneas, 3 servicios extras
Línea 1: Producto A (id=100), 10 unidades
Línea 2: Producto B (id=101), 5 unidades

Servicio 1: Urgencia - $50
Servicio 2: Programación - $100
Servicio 3: Envío - $200
```

### Test
```bash
# Operación compleja:
# - Modificar línea 1 (cambiar cantidad)
# - Eliminar línea 2
# - Modificar servicio 1
# - Eliminar servicio 2
# - Agregar servicio 4

POST /api/v1/ventas/pedidos/{pedido_id}/editar-mesa-control/

{
  "pedido": {
    "subtotal": "800.00",
    "gran_total": "928.00"
  },
  "detalle": [
    {
      "id": 100,
      "producto": {prod_a_id},
      "precio_unitario": "85.00",
      "tallas": [
        {
          "talla": 4,
          "cantidad": 20  # ← Modificado (era 10)
        }
      ]
    }
    // ← Línea 2 (id=101) no aparece: SE ELIMINA
  ],
  "servicios_extras": [
    {
      "nombre": "Urgencia Plus",  // ← Modificado
      "monto": "75.00"
    },
    // ← Programación no aparece: SE ELIMINA
    {
      "nombre": "Consultoría",  // ← Nuevo (no había)
      "monto": "150.00"
    }
  ]
}
```

### Verificación
```bash
✅ status_code == 200
✅ pedido.detalles.count() == 1  # 2 → 1
✅ pedido.detalles[0].tallas[0].cantidad == 20  # Modificado
✅ pedido.servicios_extras.count() == 2  # 3 → 2
✅ "Urgencia Plus" in pedido.servicios_extras.values_list("nombre", flat=True)  # Modificado
✅ "Programación" not in pedido.servicios_extras.values_list("nombre", flat=True)  # Eliminado
✅ "Consultoría" in pedido.servicios_extras.values_list("nombre", flat=True)  # Agregado

# Cotización sincronizada
✅ cotizacion.cotizaciondetalle.count() == 1
✅ cotizacion.servicios_extras.count() == 2
✅ cotizacion.subtotal == "800.00"
```

---

## Test Case 9: Deshacer cambios (re-editar)

### Preparación
```bash
# Ya ejecutado Test Case 1 (eliminó línea 2)
```

### Test
```bash
# Mesa de control re-edita para volver a agregar línea 2
POST /api/v1/ventas/pedidos/{pedido_id}/editar-mesa-control/

{
  "detalle": [
    {
      "id": {detalle_1_id},
      "producto": {prod_a_id},
      "tallas": [...]
    },
    {
      // ← Sin id: crear nueva línea 2
      "producto": {prod_b_id},
      "tallas": [...]
    }
  ]
}
```

### Verificación
```bash
✅ status_code == 200
✅ pedido.detalles.count() == 2  # Vuelve a 2
✅ cotizacion.cotizaciondetalle.count() == 2
```

---

## Comandos de Testing Rápido (Django Test)

```bash
# Ejecutar solo tests de edición mesa de control
python manage.py test ventas.tests.PedidoViewSetScopeTenantTests.test_edicion_mesa_control_actualiza_pedido_y_cotizacion_sin_inventario -v 2

# Ejecutar test que verifica permisos
python manage.py test ventas.tests.PedidoViewSetScopeTenantTests.test_usuario_mesa_control_no_admin_si_puede_abrir_contexto -v 2

# Ejecutar todos los tests de PedidoViewSet
python manage.py test ventas.tests.PedidoViewSetScopeTenantTests -v 2

# Ejecutar con cobertura
coverage run --source='ventas.api.views' manage.py test ventas.tests
coverage report -m
```

---

## Checklist de Verificación

- [ ] Test Case 1: Eliminar línea ✅
- [ ] Test Case 2: Eliminar talla ✅
- [ ] Test Case 3: Eliminar servicio extra ✅
- [ ] Test Case 4: Agregar nueva línea ✅
- [ ] Test Case 5: Modificar configuración ✅
- [ ] Test Case 6: Cambiar muestra ✅
- [ ] Test Case 7: Bloqueos siguen funcionando ✅
- [ ] Test Case 8: Combinación compleja ✅
- [ ] Test Case 9: Deshacer cambios ✅
- [ ] Sincronización con cotización funciona ✅
- [ ] Frontend reconoce flags: permite_eliminar_* = True ✅
- [ ] No hay errores 500 en logs ✅
- [ ] Auditoría actualiza timestamps ✅

---

## Notas Importantes

1. **Mesa de Control debe estar habilitada:**
   ```sql
   UPDATE roles SET clave_departamento = 'MESACONTROL' 
   WHERE codigo LIKE 'MESACONTROL%';
   ```

2. **Bloqueos siguen vigentes:** Si hay factura emitida, picking activo, etc., NO se puede editar

3. **Eliminaciones por omisión:** No se envía en payload = se elimina automáticamente

4. **Sincronización es atómico:** Si algo falla, ROLLBACK de todo (transaction.atomic)

5. **Frontend debe adaptarse:** Cambiar UI para permitir eliminar en lugar de ocultar botón

6. **Sin inventario tocado:** Ediciones no afectan existencias (ya fue descontado al autorizar)

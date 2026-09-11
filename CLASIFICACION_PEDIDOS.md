# 📊 Clasificación de Pedidos - Mesa de Control

## Resumen

Se ha agregado un nuevo campo **`clasificacion`** al pedido que **SOLO MESA DE CONTROL PUEDE EDITAR**.

Este campo determina los tiempos de entrega esperados del pedido y es visible en:
- Pedido (Ventas)
- Cotización (sincronizado automáticamente)
- Historial (simple_history)

---

## Opciones de Clasificación

| Código | Descripción | Tiempo de Entrega |
|--------|-------------|-------------------|
| **A** | 2 a 5 días | Urgente |
| **B** | 5 a 8 días | Normal |
| **C** | 5 a 15 días | Estándar |
| **D** | 4 a 6 semanas | Especial |
| **E** | 6 a 8 semanas | Largo plazo |
| **F** | 8 a 10 semanas | Extra largo |
| **X** | Solo para facturar | No producción |

---

## Implementación

### 1. Cambios en Modelos

#### `ventas/models.py` - Cotización

```python
class Cotizacion(models.Model):
    # ... campos existentes ...
    
    class Clasificacion(models.TextChoices):
        A = 'A', 'A - 2 a 5 días'
        B = 'B', 'B - 5 a 8 días'
        C = 'C', 'C - 5 a 15 días'
        D = 'D', 'D - 4 a 6 semanas'
        E = 'E', 'E - 6 a 8 semanas'
        F = 'F', 'F - 8 a 10 semanas'
        X = 'X', 'X - Solo para facturar'
    
    clasificacion = models.CharField(
        max_length=1,
        choices=Clasificacion.choices,
        null=True,
        blank=True,
        help_text="Clasificación de tiempo de entrega"
    )
```

#### `ventas/models.py` - Pedido

```python
class Pedido(StatusLifecycleModel):
    # ... campos existentes ...
    
    class Clasificacion(models.TextChoices):
        A = 'A', 'A - 2 a 5 días'
        B = 'B', 'B - 5 a 8 días'
        C = 'C', 'C - 5 a 15 días'
        D = 'D', 'D - 4 a 6 semanas'
        E = 'E', 'E - 6 a 8 semanas'
        F = 'F', 'F - 8 a 10 semanas'
        X = 'X', 'X - Solo para facturar'
    
    clasificacion = models.CharField(
        max_length=1,
        choices=Clasificacion.choices,
        null=True,
        blank=True,
        db_index=True,
        help_text="Clasificación de tiempo de entrega: A=2-5d, B=5-8d, C=5-15d, D=4-6sem, E=6-8sem, F=8-10sem, X=Solo facturar"
    )
```

### 2. Cambios en Serializers

#### `ventas/api/serializers.py` - PedidoMesaControlHeaderSerializer

```python
class PedidoMesaControlHeaderSerializer(serializers.ModelSerializer):
    clasificacion = serializers.ChoiceField(
        choices=Pedido.Clasificacion.choices,
        required=False,
        allow_null=True,
        help_text="Clasificación de tiempo de entrega"
    )
    
    class Meta:
        model = Pedido
        exclude = [
            "empresa",
            "serie_folio",
            "folio",
            "folio_consecutivo",
            "cotizacion",
            "estatus",
            "activo",
            "created_at",
            "updated_at",
            "fecha_confirmacion",
        ]
```

### 3. Sincronización Automática

#### `ventas/api/views.py` - PEDIDO_COTIZACION_MIRROR_FIELDS

```python
PEDIDO_COTIZACION_MIRROR_FIELDS = (
    # ... otros campos ...
    "gran_total",
    "clasificacion",  # ← NUEVO
)
```

Cuando mesa de control edita el pedido, el campo `clasificacion` se sincroniza automáticamente a la cotización.

### 4. Migración

```bash
Archivo: ventas/migrations/0045_cotizacion_clasificacion_and_more.py

Acciones:
- Agrega clasificacion a Cotizacion
- Agrega clasificacion a Pedido
- Agrega clasificacion a HistoricalCotizacion
- Agrega clasificacion a HistoricalPedido
```

---

## Cómo Usar

### 1. Obtener Contexto de Edición

```bash
GET /api/v1/ventas/pedidos/{id}/editar-mesa-control-contexto/

Response:
{
  "editable": true,
  "permite_eliminar_renglones": true,
  "bloqueos": []
}
```

### 2. Cargar Pedido Completo

```bash
GET /api/v1/ventas/pedidos/{id}/

Response:
{
  "id": 123,
  "folio": "P-000001",
  "clasificacion": null,  # ← Campo actual (null si no está asignada)
  "cliente": 15,
  ...
}
```

### 3. Editar Pedido (Agregar/Cambiar Clasificación)

```bash
POST /api/v1/ventas/pedidos/{id}/editar-mesa-control/

Body:
{
  "pedido": {
    "sucursal": 1,
    "cliente": 15,
    "clasificacion": "B",  # ← Asignar clasificación B (5-8 días)
    "subtotal": "850.00",
    "gran_total": "986.00"
  },
  "detalle": [...],
  "servicios_extras": []
}

Response:
{
  "pedido": {
    "id": 123,
    "clasificacion": "B",  # ← Actualizado
    ...
  },
  "cotizacion": {
    "id": 456,
    "clasificacion": "B",  # ← Sincronizado automáticamente
    ...
  },
  "sincronizado": true,
  "modo": "estricto_contable_operativo"
}
```

### 4. Cambiar Clasificación Posteriormente

Mesa de Control puede reasignar la clasificación en cualquier momento (si no hay bloqueos):

```bash
POST /api/v1/ventas/pedidos/{id}/editar-mesa-control/

Body:
{
  "pedido": {
    "clasificacion": "D"  # ← Cambiar de B a D (4-6 semanas)
  },
  "detalle": [...],  # Mantener como está
  "servicios_extras": [...]
}
```

---

## Restricciones y Validaciones

### Quién Puede Editar

- ✅ Mesa de Control (solo ellos)
- ❌ Vendedor (no puede editar)
- ❌ Admin empresa (no puede editar desde vendedor, pero SÍ como mesa de control)
- ❌ Usuarios sin rol de mesa de control

### Cuándo NO Puede Editar

Si el pedido tiene bloqueos:
- Factura emitida
- Nota de crédito ligada
- Orden de producción activa
- Picking activo
- etc.

**Resultado:** Retorna 409 CONFLICT (ver bloqueos, resolver primero)

### Valores Permitidos

```python
'A'  # 2 a 5 días
'B'  # 5 a 8 días
'C'  # 5 a 15 días
'D'  # 4 a 6 semanas
'E'  # 6 a 8 semanas
'F'  # 8 a 10 semanas
'X'  # Solo para facturar
null # Sin clasificación asignada
```

---

## Ejemplos Completos

### Ejemplo 1: Asignar Clasificación A (Urgente)

**Escenario:** Pedido recién autorizado, sin clasificación

```bash
POST /api/v1/ventas/pedidos/123/editar-mesa-control/

{
  "pedido": {
    "sucursal": 1,
    "cliente": 15,
    "moneda": 1,
    "tipo_pedido": 1,
    "persona_pagos": "Juan Pérez",
    "correo_facturas": "juan@cliente.com",
    "telefono_pagos": "5551234567",
    "forma_pago": "03",
    "metodo_pago": "PUE",
    "uso_cfdi": "G03",
    "subtotal": "1000.00",
    "iva": 16,
    "gran_total": "1160.00",
    "clasificacion": "A"  # ← URGENTE (2-5 días)
  },
  "detalle": [
    {
      "id": 100,
      "producto": 77,
      "precio_unitario": "100.00",
      "tallas": [
        { "talla": 4, "cantidad": 10 }
      ]
    }
  ],
  "servicios_extras": []
}

Response: 200 OK
{
  "pedido": {
    "clasificacion": "A"  ✅
  },
  "cotizacion": {
    "clasificacion": "A"  ✅ Sincronizado
  }
}
```

---

### Ejemplo 2: Cambiar de Clasificación B a D

**Escenario:** Pedido que se creó como normal (B), pero se necesita más tiempo (D)

```bash
POST /api/v1/ventas/pedidos/456/editar-mesa-control/

{
  "pedido": {
    "clasificacion": "D"  # ← Cambiar de B (5-8d) a D (4-6sem)
  },
  "detalle": [
    { "id": 200, "producto": 78, "tallas": [...] }
  ],
  "servicios_extras": [
    { "nombre": "Urgencia", "monto": "50.00" }
  ]
}

Response: 200 OK
{
  "pedido": {
    "id": 456,
    "clasificacion": "D",  ✅
    "updated_at": "2026-09-11T18:50:00Z"
  },
  "cotizacion": {
    "id": 789,
    "clasificacion": "D",  ✅ Sincronizado
    "updated_at": "2026-09-11T18:50:00Z"
  },
  "sincronizado": true
}
```

---

### Ejemplo 3: Marcar como "Solo Facturar" (X)

**Escenario:** Pedido que no tiene producción, solo facturación

```bash
POST /api/v1/ventas/pedidos/789/editar-mesa-control/

{
  "pedido": {
    "clasificacion": "X"  # ← SOLO PARA FACTURAR
  },
  "detalle": [
    { "id": 300, "producto": 79, "tallas": [...] }
  ],
  "servicios_extras": []
}

Response: 200 OK
{
  "pedido": {
    "clasificacion": "X"  ✅
  },
  "cotizacion": {
    "clasificacion": "X"  ✅
  }
}
```

---

### Ejemplo 4: Limpiar Clasificación (Volver a NULL)

**Escenario:** Desasignar clasificación de un pedido

```bash
POST /api/v1/ventas/pedidos/999/editar-mesa-control/

{
  "pedido": {
    "clasificacion": null  # ← Desasignar
  },
  "detalle": [...],
  "servicios_extras": [...]
}

Response: 200 OK
{
  "pedido": {
    "clasificacion": null  ✅
  },
  "cotizacion": {
    "clasificacion": null  ✅
  }
}
```

---

## Auditoría y Historial

### Historial de Cambios

La clasificación se registra en el historial de simple_history:

```sql
SELECT 
  id, 
  pedido_id, 
  clasificacion, 
  history_date, 
  history_change_reason 
FROM pedido_historicalpedido 
WHERE pedido_id = 123 
ORDER BY history_date DESC;

-- Resultado:
id  | pedido_id | clasificacion | history_date           | history_change_reason
1   | 123       | D             | 2026-09-11 18:50:00    | NULL
2   | 123       | B             | 2026-09-11 17:30:00    | NULL
3   | 123       | NULL          | 2026-09-11 16:00:00    | NULL
```

### Auditoría

Cada cambio de clasificación:
- Actualiza `Pedido.updated_at`
- Actualiza `Cotizacion.updated_at` (automático vía sincronización)
- Se registra en `HistoricalPedido` (simple_history)

---

## Consultas SQL Útiles

### Ver pedidos por clasificación

```sql
SELECT 
  id, 
  folio, 
  cliente_nombre, 
  clasificacion, 
  created_at 
FROM pedidos 
WHERE clasificacion = 'A' 
ORDER BY created_at DESC;
```

### Ver cambios de clasificación

```sql
SELECT 
  hp.pedido_id, 
  hp.clasificacion, 
  hp.history_date 
FROM pedido_historicalpedido hp
WHERE hp.pedido_id = 123
ORDER BY hp.history_date DESC
LIMIT 5;
```

### Pedidos sin clasificación

```sql
SELECT 
  id, 
  folio, 
  cliente_nombre 
FROM pedidos 
WHERE clasificacion IS NULL 
AND estatus = 3 
ORDER BY created_at DESC;
```

---

## Cambios en Frontend

El frontend debe:

1. **Mostrar el campo clasificación en la pantalla de edición:**
   ```typescript
   <select name="clasificacion" value={pedido.clasificacion || ''}>
     <option value="">Sin clasificación</option>
     <option value="A">A - 2 a 5 días</option>
     <option value="B">B - 5 a 8 días</option>
     <option value="C">C - 5 a 15 días</option>
     <option value="D">D - 4 a 6 semanas</option>
     <option value="E">E - 6 a 8 semanas</option>
     <option value="F">F - 8 a 10 semanas</option>
     <option value="X">X - Solo para facturar</option>
   </select>
   ```

2. **Incluir el valor en el payload al guardar:**
   ```typescript
   const payload = {
     pedido: {
       ...
       clasificacion: form.clasificacion
     },
     detalle: [...],
     servicios_extras: [...]
   }
   ```

3. **Mostrar la clasificación en el detalle del pedido:**
   ```typescript
   <p>Clasificación: {pedido.clasificacion ? 
     CLASIFICACION_LABELS[pedido.clasificacion] : 
     'No asignada'}</p>
   ```

---

## API Endpoint Summary

| Endpoint | Método | Campo | Acceso |
|----------|--------|-------|--------|
| `/editar-mesa-control-contexto/` | GET | (info) | Mesa de Control |
| `/editar-mesa-control/` | POST | `clasificacion` | Mesa de Control |
| `/` (retrieve) | GET | `clasificacion` | Lectura (todos) |

---

## Consideraciones Técnicas

- **Index:** Pedido.clasificacion tiene `db_index=True` para consultas rápidas
- **Nullable:** Permite NULL para pedidos sin clasificación asignada
- **Choices:** Validación a nivel de Django (no acepta valores inválidos)
- **Sincronización:** Automática en `_aplicar_pedido_a_cotizacion()`
- **Historial:** Tracked by simple_history en ambas tablas

---

## Migración y Deploy

### Paso 1: Aplicar migración

```bash
python manage.py migrate ventas
```

### Paso 2: Verificar

```bash
python manage.py dbshell

-- Verificar que la columna existe
SELECT column_name, data_type 
FROM information_schema.columns 
WHERE table_name='pedidos' AND column_name='clasificacion';
```

### Paso 3: Teste los endpoints

```bash
# Obtener contexto
curl -X GET "http://localhost:8000/api/v1/ventas/pedidos/1/editar-mesa-control-contexto/"

# Editar con clasificación
curl -X POST "http://localhost:8000/api/v1/ventas/pedidos/1/editar-mesa-control/" \
  -H "Content-Type: application/json" \
  -d '{
    "pedido": {"clasificacion": "B"},
    "detalle": [...],
    "servicios_extras": [...]
  }'
```

---

## Resumen

✅ **Implementación completa:**
- Campo agregado a Pedido y Cotización
- Serializer actualizado (PedidoMesaControlHeaderSerializer)
- Sincronización automática en PEDIDO_COTIZACION_MIRROR_FIELDS
- Migración creada (0045_cotizacion_clasificacion_and_more.py)
- Auditoría completa (simple_history)

✅ **Solo Mesa de Control puede editar**

✅ **Sincroniza automáticamente con Cotización**

✅ **Listo para producción**

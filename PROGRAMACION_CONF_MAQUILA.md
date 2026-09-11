# 🎯 Programación de Maquila (Múltiples Parcialidades)

## Resumen Ejecutivo

Se cambió la estructura de programación de pedidos para usar un único campo **`programacion_conf`** (JSONField) que permite guardar **múltiples parcialidades** dentro de una sola línea.

**Antes:** Campos separados para cada tipo de surtido
```python
fecha_embarque = DateField()
cantidad_embarque = IntegerField()
fecha_surtir_bordado = DateField()
cantidad_surtir_bordado = IntegerField()
fecha_surtir_apartados = DateField()
cantidad_surtir_apartados = IntegerField()
```

**Ahora:** Un solo campo JSON flexible
```python
programacion_conf = JSONField()  # Contiene múltiples programaciones
```

---

## Estructura del JSON

### Formato Básico

```json
{
  "programaciones": [
    {
      "fecha": "2026-09-20",
      "cantidad": 50
    },
    {
      "fecha": "2026-09-25",
      "cantidad": 30
    },
    {
      "fecha": "2026-09-30",
      "cantidad": 20
    }
  ]
}
```

### Ejemplo Real (Pedido para Maquila)

```json
{
  "programaciones": [
    {
      "fecha": "2026-09-15",
      "cantidad": 100,
      "etapa": "corte",
      "observaciones": "Prioridad normal"
    },
    {
      "fecha": "2026-09-20",
      "cantidad": 50,
      "etapa": "bordado",
      "observaciones": "Urgente"
    },
    {
      "fecha": "2026-09-25",
      "cantidad": 40,
      "etapa": "empaque",
      "observaciones": "Último lote"
    },
    {
      "fecha": "2026-09-30",
      "cantidad": 10,
      "etapa": "entrega",
      "observaciones": "Muestras"
    }
  ]
}
```

---

## Ventajas de Esta Estructura

### ✅ Múltiples Parcialidades en Una Línea
```json
{
  "programaciones": [
    {"fecha": "2026-09-15", "cantidad": 100},  // Primera parcialidad
    {"fecha": "2026-09-20", "cantidad": 50},   // Segunda parcialidad
    {"fecha": "2026-09-30", "cantidad": 50}    // Tercera parcialidad
  ]
}
// Total: 200 unidades en 3 entregas
```

### ✅ Flexible y Escalable
Puedes agregar campos adicionales sin cambiar el esquema:
```json
{
  "programaciones": [
    {
      "fecha": "2026-09-20",
      "cantidad": 50,
      "etapa": "bordado",
      "linea_produccion": "Línea 2",
      "responsable": "Juan",
      "observaciones": "Urgente",
      "numero_lote": "LOT-001"
    }
  ]
}
```

### ✅ Sin Tablas Adicionales
Todo en una columna → Más simple, más rápido

### ✅ Auditable
El historial (simple_history) registra cada cambio del JSON

---

## Cómo Usar desde Mesa de Control

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

### 2. Cargar Pedido Actual

```bash
GET /api/v1/ventas/pedidos/{id}/

Response:
{
  "id": 123,
  "folio": "P-000001",
  "programacion_conf": null,  # ← Vacío si no hay programación
  ...
}
```

### 3. Editar Agregando Programación

```bash
POST /api/v1/ventas/pedidos/{id}/editar-mesa-control/

{
  "pedido": {
    "sucursal": 1,
    "cliente": 15,
    "subtotal": "5000.00",
    "gran_total": "5800.00",
    "programacion_conf": {
      "programaciones": [
        {
          "fecha": "2026-09-20",
          "cantidad": 100
        },
        {
          "fecha": "2026-09-25",
          "cantidad": 75
        },
        {
          "fecha": "2026-09-30",
          "cantidad": 25
        }
      ]
    }
  },
  "detalle": [
    {
      "id": 100,
      "producto": 77,
      "tallas": [{ "talla": 4, "cantidad": 200 }]
    }
  ],
  "servicios_extras": []
}

Response: 200 OK
{
  "pedido": {
    "id": 123,
    "programacion_conf": {
      "programaciones": [
        {"fecha": "2026-09-20", "cantidad": 100},
        {"fecha": "2026-09-25", "cantidad": 75},
        {"fecha": "2026-09-30", "cantidad": 25}
      ]
    },
    "updated_at": "2026-09-11T19:30:00Z"
  },
  "cotizacion": {
    "programacion_conf": { ... }  // Sincronizado automáticamente
  },
  "sincronizado": true
}
```

### 4. Modificar Programación Posteriormente

Agregar una parcialidad más:

```bash
POST /api/v1/ventas/pedidos/{id}/editar-mesa-control/

{
  "pedido": {
    "programacion_conf": {
      "programaciones": [
        {"fecha": "2026-09-20", "cantidad": 100},
        {"fecha": "2026-09-25", "cantidad": 75},
        {"fecha": "2026-09-30", "cantidad": 25},
        {"fecha": "2026-10-05", "cantidad": 50}  # ← Nueva parcialidad
      ]
    }
  },
  "detalle": [...],
  "servicios_extras": [...]
}
```

Cambiar cantidades:

```bash
{
  "pedido": {
    "programacion_conf": {
      "programaciones": [
        {"fecha": "2026-09-20", "cantidad": 150},  # ← Cambié de 100 a 150
        {"fecha": "2026-09-25", "cantidad": 75},
        {"fecha": "2026-09-30", "cantidad": 25}
      ]
    }
  },
  "detalle": [...],
  "servicios_extras": [...]
}
```

### 5. Limpiar Programación (Dejar Vacío)

```bash
{
  "pedido": {
    "programacion_conf": null  # ← O {}
  },
  "detalle": [...],
  "servicios_extras": [...]
}
```

---

## Casos de Uso Reales (Maquila de Ropa)

### Caso 1: Pedido Grande con 3 Entregas

**Producto:** 200 Polos XL - Bordado
**Entrega:** En 3 parcialidades según disponibilidad de maquila

```json
{
  "programacion_conf": {
    "programaciones": [
      {
        "fecha": "2026-09-15",
        "cantidad": 100,
        "etapa": "corte",
        "linea_produccion": "Línea 1",
        "observaciones": "Primera tanda"
      },
      {
        "fecha": "2026-09-20",
        "cantidad": 60,
        "etapa": "bordado",
        "linea_produccion": "Línea 2",
        "observaciones": "Disponible después del 18"
      },
      {
        "fecha": "2026-09-28",
        "cantidad": 40,
        "etapa": "empaque",
        "linea_produccion": "Línea 1",
        "observaciones": "Último lote, urgente"
      }
    ]
  }
}
```

### Caso 2: Muestras Progresivas

**Producto:** Prototipo Pantalón - Cambio de Talla
**Entrega:** Muestras de cada talla en diferentes fechas

```json
{
  "programacion_conf": {
    "programaciones": [
      {
        "fecha": "2026-09-18",
        "cantidad": 2,
        "observaciones": "Talla XS - Muestra"
      },
      {
        "fecha": "2026-09-20",
        "cantidad": 2,
        "observaciones": "Talla S - Muestra"
      },
      {
        "fecha": "2026-09-22",
        "cantidad": 2,
        "observaciones": "Talla M - Muestra"
      },
      {
        "fecha": "2026-09-25",
        "cantidad": 2,
        "observaciones": "Talla L - Muestra"
      }
    ]
  }
}
```

### Caso 3: Pedido con Entregas Semanales

**Producto:** 500 Camisetas
**Entrega:** Semanal durante un mes

```json
{
  "programacion_conf": {
    "programaciones": [
      {
        "fecha": "2026-09-18",
        "cantidad": 125,
        "semana": "Semana 1"
      },
      {
        "fecha": "2026-09-25",
        "cantidad": 125,
        "semana": "Semana 2"
      },
      {
        "fecha": "2026-10-02",
        "cantidad": 125,
        "semana": "Semana 3"
      },
      {
        "fecha": "2026-10-09",
        "cantidad": 125,
        "semana": "Semana 4"
      }
    ]
  }
}
```

---

## Base de Datos

### Consultar Programación

```sql
SELECT 
  id, 
  folio, 
  programacion_conf,
  updated_at 
FROM pedidos 
WHERE programacion_conf IS NOT NULL
ORDER BY updated_at DESC;

-- Resultado:
id  | folio     | programacion_conf                                      | updated_at
123 | P-000001  | {"programaciones": [{"fecha": "2026-09-20", ...}]}    | 2026-09-11 19:30:00
124 | P-000002  | {"programaciones": [{"fecha": "2026-09-15", ...}]}    | 2026-09-11 18:00:00
```

### Filtrar por Fecha de Programación

```sql
-- Pedidos con programación para septiembre 20
SELECT 
  id, 
  folio,
  programacion_conf -> 'programaciones' @> '[{"fecha": "2026-09-20"}]'::jsonb
FROM pedidos
WHERE programacion_conf @> '{"programaciones": [{"fecha": "2026-09-20"}]}'::jsonb;
```

### Extraer Cantidad Total Programada

```sql
SELECT 
  id, 
  folio,
  (SELECT SUM((prog->>'cantidad')::integer)
   FROM jsonb_array_elements(programacion_conf->'programaciones') prog) as cantidad_total
FROM pedidos
WHERE programacion_conf IS NOT NULL;
```

---

## Validación y Estructura Esperada

### Validación en Frontend

```typescript
interface Programacion {
  fecha: string;          // YYYY-MM-DD
  cantidad: number;       // > 0
  etapa?: string;         // Opcional: corte, bordado, empaque, etc.
  linea_produccion?: string;  // Opcional: Línea 1, Línea 2, etc.
  responsable?: string;   // Opcional: Nombre del responsable
  observaciones?: string; // Opcional: Notas adicionales
}

interface ProgramacionConf {
  programaciones: Programacion[];
}

// Validación
function validarProgramacion(conf: ProgramacionConf): boolean {
  if (!conf?.programaciones || !Array.isArray(conf.programaciones)) {
    return false;
  }
  
  return conf.programaciones.every(p => {
    const fecha = new Date(p.fecha);
    return !isNaN(fecha.getTime()) && p.cantidad > 0;
  });
}
```

### Validación en Backend

Django valida automáticamente:
- ✅ JSONField acepta cualquier estructura válida
- ✅ Serializer valida según configuración
- ✅ tipos de datos (fecha formato, cantidad número)

---

## Auditoría y Historial

### Ver Cambios Históricos

```sql
SELECT 
  id,
  pedido_id,
  programacion_conf,
  history_date
FROM pedido_historicalpedido
WHERE pedido_id = 123
ORDER BY history_date DESC
LIMIT 10;

-- Resultado:
id | pedido_id | programacion_conf                                           | history_date
1  | 123       | {"programaciones": [{"fecha": "2026-09-20", "cantidad": 100}]} | 2026-09-11 19:30:00
2  | 123       | {"programaciones": [{"fecha": "2026-09-20", "cantidad": 75}]}  | 2026-09-11 19:00:00
3  | 123       | null                                                            | 2026-09-11 16:00:00
```

### Versión Anterior

simple_history guarda cada versión del JSON, permitiendo ver exactamente qué cambió:
- Cambio de cantidad: ✅ Registrado
- Agregar parcialidad: ✅ Registrado
- Eliminar parcialidad: ✅ Registrado

---

## Migración y Deploye

### Paso 1: Ver Cambios

```bash
python manage.py makemigrations --dry-run
```

Salida:
```
- Remove field cantidad_embarque from pedido
- Remove field fecha_embarque from pedido
- Remove field cantidad_surtir_bordado from pedido
- Remove field fecha_surtir_bordado from pedido
- Remove field cantidad_surtir_apartados from pedido
- Remove field fecha_surtir_apartados from pedido
+ Add field programacion_conf to pedido
+ Add field programacion_conf to cotizacion
```

### Paso 2: Crear Migración

```bash
python manage.py makemigrations ventas
```

Archivo: `ventas/migrations/0046_remove_historicalpedido_cantidad_embarque_and_more.py`

### Paso 3: Aplicar Migración

```bash
python manage.py migrate ventas
```

### Paso 4: Verificar

```bash
python manage.py dbshell

-- Verificar estructura
DESCRIBE pedidos;  # o SELECT * FROM information_schema.columns WHERE table_name='pedidos';

-- Debe mostrar:
programacion_conf | json
```

---

## Cambios de API

### Campo que Se Removió

```javascript
// ❌ ANTES: 6 campos separados
{
  "fecha_embarque": "2026-09-25",
  "cantidad_embarque": 100,
  "fecha_surtir_bordado": "2026-09-20",
  "cantidad_surtir_bordado": 50,
  "fecha_surtir_apartados": null,
  "cantidad_surtir_apartados": null
}

// ✅ AHORA: 1 campo JSON flexible
{
  "programacion_conf": {
    "programaciones": [
      {"fecha": "2026-09-20", "cantidad": 50},
      {"fecha": "2026-09-25", "cantidad": 100}
    ]
  }
}
```

### Compatible con Sincronización

✅ `programacion_conf` está en `PEDIDO_COTIZACION_MIRROR_FIELDS`
- Cambios en Pedido → Sincroniza con Cotización
- Historial completo en ambas tablas

---

## Ventajas Finales

| Aspecto | Antes | Ahora |
|---------|-------|-------|
| **Múltiples parcialidades** | ❌ No | ✅ Sí, sin límite |
| **Campos en BD** | 6 campos | 1 campo JSON |
| **Flexibilidad** | Limitada | Completa |
| **Escalabilidad** | Limitada (requiere ALTER TABLE) | Completa (solo JSON) |
| **Auditoría** | ✅ simple_history | ✅ Mejorada (JSON tracking) |
| **Índices** | ✅ btree | ✅ jsonb index posible |
| **Sincronización** | N/A | ✅ Automática |

---

## Conclusión

✅ **Estructura Nueva Implementada**

**Antes:** 6 campos separados (limitado a 1 parcialidad por tipo)
**Ahora:** 1 campo JSON (múltiples parcialidades, flexible, escalable)

**Problema Resuelto:** ¡Múltiples parcialidades en una sola línea del encabezado! 🎉

**Status:** ✅ Listo para deploy
**Migración:** 0046_remove_historicalpedido_cantidad_embarque_and_more.py
**Sincronización:** Automática con Cotización
**Auditoría:** Completa con simple_history

---

**¡Genio? ¡Definitivamente!** 🧠✨

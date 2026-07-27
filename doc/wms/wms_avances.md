# WMS - Warehouse Management System

El módulo WMS administra la operación física del almacén.

Inventarios responde:

- ¿Cuánto tengo?
- ¿Dónde está?
- ¿Cuál es mi existencia?

WMS responde:

- ¿Quién movió la mercancía?
- ¿Cómo llegó a esa ubicación?
- ¿Cómo se surtió el pedido?
- ¿Cómo salió del almacén?
- ¿Quién realizó cada operación?

---

# Fase WMS 0 - Fundaciones del Almacén

## Objetivo

Construir la estructura física y lógica del almacén.

- [x] ¿TERMINADO?
- [x] almacenes
- [x] ubicaciones
- [x] existencias
- [x] movimientos_inventario
- [x] movimiento_inventario_detalle
- [x] ajustes_inventario
- [x] ajuste_detalle
- [x] lotes
- [x] series

Objetivo:

Que cualquier movimiento dentro del ERP tenga una ubicación física perfectamente identificada y completamente trazable.

---

# Fase WMS 1 - Movimientos Internos

## Objetivo

Administrar los movimientos físicos dentro del almacén.

- [x] ¿TERMINADO?
- [x] transferencias
- [x] transferencia_detalle

Objetivo:

Mover mercancía entre:

- almacenes
- ubicaciones
- racks
- zonas
- pasillos

Todo movimiento deberá generar automáticamente un MovimientoInventario para mantener la trazabilidad.

---

# Fase WMS 2 - Reservas de Inventario

## Objetivo

Apartar mercancía antes del surtido.

- [ ] ¿TERMINADO?
- [x] inventario_reservas

Objetivo:

Evitar sobreventa y garantizar disponibilidad para pedidos autorizados.

Estado actual:

- Ya existe la tabla `inventario_reservas`.
- La reserva se genera automáticamente al crear un `Picking`.
- Cada reserva queda ligada a:
  - `pedido_detalle`
  - `pedido_detalle_talla`
  - `existencia`
  - `almacen`
  - `ubicacion`
- La reserva nace en estado `ACTIVA`.
- Cuando el picking se genera correctamente y la transferencia a `APARTADOS` se completa, la reserva se marca como `APLICADA`.

---

# Fase WMS 3 - Picking

## Objetivo

Surtir pedidos.

- [ ] ¿TERMINADO?
- [x] picking
- [x] picking_detalle

Funciones

- [x] generar picking desde Pedido
- [x] surtido por operador
- surtido por zonas
- [x] surtido por prioridad
- surtido por oleadas (Wave Picking)
- validación de cantidades

Objetivo:

Convertir un Pedido en mercancía preparada para empaque.

Estado actual:

- Ya existe endpoint para crear, listar y consultar pickings.
- Ya existe onboarding para consultar pedido, operadores, almacenes y cantidades pendientes por talla.
- El frontend define las cantidades reales a surtir por cada línea/talla.
- El backend valida que cada cantidad no exceda lo pendiente según el historial de `PickingDetalle`.
- Antes de crear el picking, el backend genera reservas de inventario solo por las cantidades seleccionadas.
- Al crear el picking, el sistema genera una transferencia al almacén `APARTADOS` del mismo contexto para preparar surtido.
- El `Pedido` permanece como referencia comercial y no guarda el avance del surtido.
- El avance se calcula consultando el historial de `PickingDetalle`.
- La Fase WMS 2 queda conectada con la Fase WMS 3: reserva primero, mueve después, y deja trazabilidad de la mercancía apartada para surtido parcial o total.

---

# Fase WMS 4 - Packing

## Objetivo

Empacar mercancía.

- [ ] ¿TERMINADO?
- [ ] packing
- [ ] packing_detalle

Funciones

- crear cajas
- consolidar pedidos
- dividir pedidos
- peso
- volumen
- etiquetas
- códigos de barras

Objetivo:

Preparar físicamente la mercancía para embarque.

---

# Fase WMS 5 - Despachos

## Objetivo

Liberar mercancía hacia logística.

- [x] ¿TERMINADO?
- [x] despachos
- [x] despacho_detalle

Objetivo:

Cerrar la operación del almacén y entregar la mercancía al módulo de Logística.

Estado actual:

- Ya existe endpoint para crear, listar y consultar despachos.
- El flujo queda ligado exactamente como en `dbdiagram.io`: `packing -> despacho -> despacho_detalle -> envio`.
- El onboarding de despacho trabaja desde un `packing` y devuelve:
  - `envios` disponibles del mismo pedido
  - líneas `packing_detalle` pendientes por despachar
  - validación para evitar redespachar la misma línea
- El frontend solo envía:
  - `packing`
  - `envio`
  - las líneas `packing_detalle` que realmente saldrán en ese despacho
- El backend valida empresa, sucursal y que el `envio` corresponda al mismo `pedido` del `packing`.

---

# Fase WMS 6 - Conteos de Inventario

## Objetivo

Auditar el inventario físico.

- [ ] ¿TERMINADO?
- [ ] conteos_ciclicos
- [ ] conteo_ciclico_detalle

Funciones

- conteo parcial
- conteo total
- conteo por zona
- conteo ABC
- diferencias
- generación automática de ajustes

Objetivo:

Mantener la confiabilidad del inventario.

---

# Fase WMS 7 - Optimización del Almacén

## Objetivo

Reducir tiempos de operación.

- [ ] Picking por zonas
- [ ] Picking por oleadas
- [ ] Picking por lote
- [ ] Batch Picking
- [ ] Cluster Picking
- [ ] Reabastecimiento automático
- [ ] Cross Dock
- [ ] FIFO
- [ ] FEFO
- [ ] LIFO
- [ ] Clasificación ABC
- [ ] Reubicación sugerida

Objetivo:

Optimizar la productividad del almacén.

---

# Fase WMS 8 - Movilidad

## Objetivo

Eliminar capturas manuales.

- [ ] Picking móvil
- [ ] Packing móvil
- [ ] Recepción móvil
- [ ] Conteos móviles
- [ ] Transferencias móviles
- [ ] Aplicación Android
- [ ] Aplicación iOS
- [ ] Escaneo QR
- [ ] Escaneo Código de Barras

Objetivo:

Operar el almacén desde dispositivos móviles.

---

# Fase WMS 9 - RFID

## Objetivo

Automatizar completamente la identificación y trazabilidad de mercancía.

- [ ] Catálogo de etiquetas RFID
- [ ] Relación RFID ↔ Producto
- [ ] Relación RFID ↔ Serie
- [ ] Relación RFID ↔ Lote
- [ ] Lectores RFID
- [ ] Antenas RFID
- [ ] Portales RFID
- [ ] Recepción automática
- [ ] Transferencias automáticas
- [ ] Picking con RFID
- [ ] Packing con RFID
- [ ] Conteos automáticos
- [ ] Inventario en tiempo real
- [ ] Alertas de salida no autorizada
- [ ] Localización en tiempo real

Objetivo:

Eliminar prácticamente toda captura manual del almacén mediante identificación automática.

---

# Fase WMS 10 - Dashboard Operativo

## Objetivo

Administrar el almacén en tiempo real.

- [ ] Dashboard Operativo
- [ ] Picking pendientes
- [ ] Packing pendientes
- [ ] Despachos pendientes
- [ ] Conteos abiertos
- [ ] Transferencias abiertas
- [ ] Recepciones pendientes
- [ ] Productividad por operador
- [ ] Tiempo promedio de surtido
- [ ] Tiempo promedio de embarque
- [ ] KPIs del almacén

Objetivo:

Controlar toda la operación desde un único Centro de Mando WMS.

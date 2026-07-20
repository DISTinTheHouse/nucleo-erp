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
- [ ] inventario_reservas

Objetivo:

Evitar sobreventa y garantizar disponibilidad para pedidos autorizados.

---

# Fase WMS 3 - Picking

## Objetivo

Surtir pedidos.

- [ ] ¿TERMINADO?
- [ ] picking
- [ ] picking_detalle

Funciones

- generar picking desde Pedido
- surtido por operador
- surtido por zonas
- surtido por prioridad
- surtido por oleadas (Wave Picking)
- validación de cantidades

Objetivo:

Convertir un Pedido en mercancía preparada para empaque.

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

- [ ] ¿TERMINADO?
- [ ] despachos
- [ ] despacho_detalle

Objetivo:

Cerrar la operación del almacén y entregar la mercancía al módulo de Logística.

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
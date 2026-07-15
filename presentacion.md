# 🚀 Presentación de Módulos del ERP Core (Django + DRF)

Este documento presenta el funcionamiento lógico, técnico y transaccional de los cinco módulos operativos clave del sistema. Explica en detalle qué **funciones** cumple cada sección, el flujo de procesos y **qué pasa exactamente en la base de datos y el inventario** cuando se realiza cada acción.

---

## 🧭 Índice de Módulos
1. [📦 WMS (Gestión de Almacenes e Inventarios)](#1--wms-gestión-de-almacenes-e-inventarios)
2. [🧾 COMPRAS (Abastecimiento y Recepción)](#2--compras-abastecimiento-y-recepción)
3. [🏭 PRODUCCIÓN (Manufactura y BOM)](#3--producción-manufactura-y-bom)
4. [🛒 VENTAS (Cotizaciones, Pedidos y Ciclo de Demanda)](#4--ventas-cotizaciones-pedidos-y-ciclo-de-demanda)
5. [💰 CxC - CUENTAS POR COBRAR (Finanzas y Contabilidad)](#5--cxc---cuentas-por-cobrar-finanzas-y-contabilidad)

---

## 1. 📦 WMS (Gestión de Almacenes e Inventarios)

El módulo WMS (Warehouse Management System) es el encargado de salvaguardar y auditar la **"Fuente de la Verdad"** de las existencias físicas. No permite supuestos: valida que todo stock se asocie a una sucursal, almacén, producto, variante (opcional) y ubicación específica.

### ⚙️ Funciones Principales
* **Control de Stock en Tiempo Real:** Gestión de existencias exactas mediante la tabla `Existencia`.
* **Aislamiento Multi-tenant:** Los usuarios de una empresa solo ven el stock de su empresa; el sistema bloquea lecturas y escrituras cruzadas.
* **Trazabilidad de Movimientos:** Registro histórico inalterable de cada evento físico.
* **Reportes de Auditoría de Cierre:** Reportes valorizados (costo promedio) por periodo y filtros por almacén.

---

### 🔄 Flujos Operativos: ¿Qué pasa cuando se realiza la acción?

#### A. Acción: Registrar Operación de ENTRADA Manual o por Recepción
* **El Proceso:** Se notifica la llegada de mercancía.
* **Consecuencias en el Sistema:**
  1. El sistema localiza el registro en la tabla `Existencia` para esa sucursal, almacén, producto, variante y ubicación. Si no existe, lo crea con cantidad inicial de `0`.
  2. **Incrementa** la cantidad del registro (`Existencia.cantidad += cantidad_operada`).
  3. Inserta un encabezado en `MovimientoInventario` y sus renglones en `MovimientoInventarioDetalle` con la clasificación **`ENTRADA`**.
  4. Genera un registro en `AuditoriaEvento` detallando quién hizo la operación, la fecha/hora y el estado anterior vs. el nuevo.
  5. *Resultado:* El stock disponible aumenta de inmediato en el ERP.

#### B. Acción: Registrar Operación de SALIDA Manual o por Venta
* **El Proceso:** Se retira stock físico para un pedido o ajuste.
* **Consecuencias en el Sistema:**
  1. El sistema realiza una **validación estricta de stock disponible**: comprueba que la existencia actual sea mayor o igual a la cantidad que se desea retirar.
  2. **Regla de oro de seguridad:** Si el stock es insuficiente, la operación se cancela de inmediato lanzando un error `400 Bad Request` y **bloqueando la transacción** para evitar inventarios negativos.
  3. Si la validación es exitosa, **disminuye** el registro (`Existencia.cantidad -= cantidad_operada`).
  4. Registra el movimiento histórico en `MovimientoInventario` y `MovimientoInventarioDetalle` con clasificación **`SALIDA`**.
  5. Registra la trazabilidad forense en `AuditoriaEvento`.

#### C. Acción: Registrar Operación de AJUSTE Manual (Inventario Físico)
* **El Proceso:** Auditoría física donde se encuentra que el stock real no coincide con el del sistema (ej. merma o hallazgo).
* **Consecuencias en el Sistema:**
  1. El sistema no suma ni resta el delta enviado de forma directa; en su lugar, **sobrescribe** el valor anterior de la tabla de existencias con la cantidad física final indicada (`Existencia.cantidad = cantidad_ajustada`).
  2. Calcula de forma interna la diferencia aritmética para los reportes financieros.
  3. Inserta registros en `MovimientoInventario` y `MovimientoInventarioDetalle` con clasificación **`AJUSTE`**.
  4. Registra en `AuditoriaEvento` guardando el historial exacto: *"Usuario X ajustó el stock del Producto Y de 15 piezas a 12 piezas (Motivo: Merma)"*.

---

## 2. 🧾 COMPRAS (Abastecimiento y Recepción)

Gestiona el flujo de adquisición de insumos, materias primas o productos terminados con proveedores externos, controlando los impuestos y asegurando que la mercancía no ingrese al sistema sin una validación de almacén.

### ⚙️ Funciones Principales
* **Órdenes de Compra (OC):** Registro de contratos de compra, precios pactados y cálculo de impuestos (IVA).
* **Onboarding Comercial:** Flujo simplificado de alta de compras en dos fases (Cabecera -> Detalles) para evitar bloqueos por carga incompleta.
* **Recepción de Mercancía:** Evento físico y contable que controla la entrada de productos a almacenes.

---

### 🔄 Flujos Operativos: ¿Qué pasa cuando se realiza la acción?

#### A. Acción: Crear y Autorizar una Orden de Compra (OC)
```
[ Crear OC (Estatus: Pendiente) ] ➡️ [ Agregar Productos/Precios ] ➡️ [ Autorizar OC (Se bloquea edición) ]
*(Nota: El stock NO cambia en esta fase)*
```
* **El Proceso:** Compras genera la solicitud al proveedor.
* **Consecuencias en el Sistema:**
  1. Se guarda el registro de la compra con estatus **`Pendiente a confirmar`**.
  2. El backend calcula automáticamente el Subtotal, aplica la tasa de impuesto configurada (ej. `IVA 16%`) y genera el Gran Total de forma exacta en base de datos.
  3. **Impacto en Inventario:** **NINGUNO**. Las órdenes de compra representan una intención comercial/financiera, por lo que no afectan las existencias físicas.
  4. Al autorizar la OC, su estatus cambia a **`Autorizada`** y el backend **bloquea por completo su edición**. Ya no se pueden alterar precios ni cantidades para evitar fraudes.

#### B. Acción: Registrar una Recepción de Mercancía (Asociada a una OC)
* **El Proceso:** El camión del proveedor llega al almacén del ERP.
* **Consecuencias en el Sistema:**
  1. Se valida la recepción contra la OC original. El sistema permite **Recepciones Totales** o **Recepciones Parciales** (si el proveedor entrega por partes).
  2. Se genera un número de folio oficial bajo las series del almacén (`RC-XXXX`, `RT-XXXX`, etc.).
  3. **Impacto en Inventario:** Por cada producto recibido, el sistema busca su registro de `Existencia` en el almacén de destino e **incrementa su stock real** en la cantidad entregada.
  4. Genera de forma automática un `MovimientoInventario` de tipo **`ENTRADA`**, ligándolo al folio de la recepción y a la OC de origen.
  5. Si la recepción es parcial, el estatus de la OC pasa a `Recibida Parcial`; si se completó todo, pasa a `Recibida` (quedando cerrada para futuras recepciones).
  6. Se escribe en `AuditoriaEvento` para registrar la entrada de mercancía.

---

## 3. 🏭 PRODUCCIÓN (Manufactura y BOM)

Este módulo controla la manufactura de prendas de vestir (bordados, serigrafías, confección) automatizando la explosión de materiales y asegurando que ninguna orden de producción se inicie sin el respaldo de materia prima en almacén.

### ⚙️ Funciones Principales
* **BOM (Lista de Materiales):** Define la "receta" exacta de insumos (hilos, telas, botones, habilitaciones) necesarios para fabricar una variante de producto.
* **Consumo Automático de Insumos:** Descuento de materias primas sin necesidad de capturarlas manualmente.
* **Control de Órdenes de Producción (OP):** Gestión del estatus y avance del taller de manufactura.

---

### 🔄 Flujos Operativos: ¿Qué pasa cuando se realiza la acción?

#### A. Acción: Confirmar e Iniciar una Orden de Producción (OP)
```
[ Crear OP ] ➡️ [ Backend busca BOM Activo ] ➡️ [ Valida Stock de Insumos ] ➡️ [ Consume Insumos (Salida Stock) ]
```
* **El Proceso:** Se decide mandar a fabricar un lote de prendas (ej. 100 camisas).
* **Consecuencias en el Sistema:**
  1. El usuario crea la OP indicando el producto terminado y la cantidad. El frontend no envía la lista de insumos.
  2. El backend **resuelve automáticamente el BOM (Lista de Materiales) activo** para ese producto y lo multiplica por la cantidad a fabricar para obtener los insumos requeridos.
  3. **Filtro de Seguridad de Stock (Explosión de Insumos):** El sistema consulta en la tabla `Existencia` el stock disponible de cada materia prima de la lista. 
     * *Si falta una sola pieza de hilos o tela:* El sistema interrumpe el proceso, rechaza la confirmación de la OP con un error descriptivo y **no se altera ningún inventario**.
  4. Si hay inventario completo de insumos, se procede con la confirmación:
     * **Resta de Inventario:** El sistema disminuye de la tabla `Existencia` la cantidad requerida de cada insumo (Materia Prima).
     * **Consumo de Producción:** Inserta un registro en `ConsumoProduccion` y sus respectivos renglones en `ConsumoDetalle`.
     * **Movimiento Histórico:** Crea un registro en `MovimientoInventario` y `MovimientoInventarioDetalle` con la clasificación **`SALIDA`** para cada materia prima, asociándolos a la OP.
  5. La OP pasa al estatus de **`En Proceso`**.

#### B. Acción: Registrar la Entrada de Producto Terminado (Fin de la OP)
* **El Proceso:** El taller termina la confección de las camisas listas para la venta.
* **Consecuencias en el Sistema:**
  1. El producto terminado no se mete al sistema por vías informales; se utiliza el flujo de **Recepciones de Mercancía** indicando que el origen es una `Orden de Producción`.
  2. El sistema valida la cantidad terminada contra la OP original.
  3. **Impacto en Inventario:** **Suma** la cantidad de prendas terminadas a la tabla `Existencia` del producto terminado.
  4. Crea un `MovimientoInventario` clasificado como **`ENTRADA`** y, de manera crucial, **liga el movimiento al ID de la Orden de Producción (`MovimientoInventario.op`)** para trazabilidad de costos y productividad.
  5. La OP cambia su estatus a **`Terminada`** o **`Recibida Parcial`** según corresponda.
  6. Se guarda la bitácora en `AuditoriaEvento`.

---

## 4. 🛒 VENTAS (Cotizaciones, Pedidos y Ciclo de Demanda)

Mapea todo el proceso comercial. Garantiza que las ventas se validen por personal administrativo (Mesa de Control) y que el inventario se aparte únicamente cuando la cotización se convierta en un pedido formal, evitando sobreventas.

### ⚙️ Funciones Principales
* **Cotizaciones Dinámicas:** Registro inicial de prospectos, precios, tallas y variantes sin comprometer inventario.
* **Validación por Mesa de Control:** Flujo de autorización para control administrativo y de riesgos.
* **Conversión Automatizada a Pedido:** Traspaso directo de la cotización a pedido activo, apartando inventario.
* **Control de Modificaciones de Pedidos:** Recálculo automático del inventario (deltas) si hay cambios autorizados en cantidades.

---

### 🔄 Flujos Operativos: ¿Qué pasa cuando se realiza la acción?

#### A. Acción: Crear una Cotización
* **El Proceso:** Un vendedor captura la cotización para un cliente en el sistema.
* **Consecuencias en el Sistema:**
  1. Se registra la cotización con estatus **`Borrador`** o **`En Revisión`**.
  2. **Impacto en Inventario:** **NINGUNO**. No se resta ni se aparta stock, garantizando que cotizaciones especulativas no bloqueen el inventario real de la empresa.

#### B. Acción: Autorización por Mesa de Control y Conversión a Pedido
```
[ Cotización en Revisión ] ➡️ [ Mesa de Control Autoriza ] ➡️ [ Valida Stock Real ] ➡️ [ Crea Pedido + Descuenta Stock (Salida) ]
```
* **El Proceso:** La Mesa de Control aprueba las condiciones comerciales y autoriza el documento.
* **Consecuencias en el Sistema:**
  1. Al hacer clic en "Autorizar", el backend realiza una **consulta de stock real** en la tabla `Existencia` para cada producto, variante y talla de la cotización.
     * *Si no hay stock suficiente:* El backend detiene la autorización y arroja un error: *"No hay existencias suficientes para el producto X"*. La cotización permanece en revisión.
  2. Si hay stock suficiente en el almacén de la sucursal:
     * **Conversión:** Se crea un nuevo registro en la tabla de **`Pedidos`** copiando todos los datos, tallas, precios y el campo `tipo_pedido`.
     * **Impacto en Inventario (SALIDA):** El sistema **descuenta inmediatamente** la cantidad de productos de la tabla `Existencia` (disminuye el stock real).
     * **Movimiento Histórico:** Inserta un registro en `MovimientoInventario` y `MovimientoInventarioDetalle` con tipo **`SALIDA`**, vinculándolos directamente con el ID del Pedido generado.
     * **Estatus:** La cotización original cambia a estatus `Autorizada` y el Pedido pasa a estar `Activo` para logística/embarque.
     * **Auditoría:** Se registra el evento en `AuditoriaEvento`.

#### C. Acción: Procesar un Cambio Solicitado sobre un Pedido Autorizado
* **El Proceso:** El cliente solicita modificar cantidades de un pedido que ya había sido autorizado.
* **Consecuencias en el Sistema:**
  1. Si la Mesa de Control acepta los cambios, el sistema evalúa la diferencia de cantidades (**Delta**):
     * **Caso A (Aumento de cantidad, ej. de 10 a 12 piezas):** El sistema calcula un delta de `+2`. Valida que existan esas 2 piezas adicionales en stock. Si hay, las descuenta de `Existencia` y genera una `SALIDA` de inventario adicional por `2`.
     * **Caso B (Disminución de cantidad, ej. de 10 a 7 piezas):** El sistema calcula un delta de `-3`. **Regresa de forma automática** esas 3 piezas a la tabla de `Existencia` e inserta una `ENTRADA` de inventario de ajuste por `3` piezas para que vuelvan a estar disponibles.
  2. Recalcula de inmediato subtotales, impuestos e importes del Pedido.
  3. Registra el desglose de los cambios aplicados en `AuditoriaEvento`.

---

## 5. 💰 CxC - CUENTAS POR COBRAR (Finanzas y Contabilidad)

Garantiza la salud financiera de la empresa y la trazabilidad fiscal. Su propósito es automatizar la contabilidad para que cada factura emitida genere inmediatamente sus repercusiones de cobranza y asientos contables de forma atómica.

### ⚙️ Funciones Principales
* **Facturación Integrada:** Factura pedidos directamente para evitar recapturas manuales.
* **Transacciones Atómicas Multi-tabla:** Al dar de alta una factura directa, el sistema escribe simultáneamente en el módulo fiscal, financiero y contable en un solo paso seguro.
* **Trazabilidad Contable:** Vinculación directa de la Cuenta por Cobrar con su respectiva Póliza Contable.

---

### 🔄 Flujos Operativos: ¿Qué pasa cuando se realiza la acción?

#### A. Acción: Facturar un Pedido Autorizado
* **El Proceso:** Se solicita la emisión del comprobante fiscal para el cliente.
* **Consecuencias en el Sistema:**
  1. El sistema valida que el pedido esté en un estatus facturable.
  2. Valida de forma estricta que **no exista otra factura activa** vinculada a ese pedido (evita la duplicidad de ingresos y facturas fiscales).
  3. Genera la factura, cambia el estatus de facturación del pedido a `Facturado` y hereda la información contable.

#### B. Acción: Registrar Factura Directa Pendiente de Cobro
```
                                        ┌── 📄 Registro de Factura
[ Registrar Factura Directa ] ➡️ ⚡ ATÓMICO ┼── 💵 Cuenta por Cobrar (CxC)
                                        └── 📊 Póliza Contable de Ingreso
```
* **El Proceso:** Se captura una factura de crédito para un cliente que no proviene de un pedido tradicional del ERP.
* **Consecuencias en el Sistema:**
  1. El usuario llena los datos de la factura. Si no escribe un folio, el sistema le asigna uno de forma secuencial.
  2. Al presionar "Guardar", el backend ejecuta una **transacción atómica** (si algo falla, nada se guarda) que realiza tres operaciones en cascada:
     * **Operación 1:** Crea el registro de la **`Factura`** con sus importes e impuestos calculados.
     * **Operación 2:** Genera una **`Cuenta por Cobrar (CxC)`** vinculada al cliente, con el saldo total pendiente y la fecha de vencimiento según sus días de crédito.
     * **Operación 3 (Póliza Contable):** Genera una **`Póliza de Ingreso/Venta`** contable de forma automática, insertando los asientos contables (cargos y abonos) a las cuentas de Clientes, Ventas e Impuestos Trasladados Pendientes, asignando además el Centro de Costos correspondiente.
  3. *Resultado de trazabilidad:* Cuando el contador revisa la Cuenta por Cobrar en su pantalla de finanzas, puede hacer clic y ver de forma inmediata tanto la factura original en PDF/XML como el asiento de la póliza contable con el que se registró en libros.
  4. Guarda el evento en `AuditoriaEvento`.

---

# 📊 Matriz de Impacto en Inventarios y Base de Datos

| Módulo | Acción del Usuario | ¿Afecta Inventarios (`Existencia`)? | Movimiento de Inventario Generado | Registro en Auditoría (`AuditoriaEvento`) | Transacciones Atómicas Multitabla / Validaciones Clave |
| :--- | :--- | :---: | :---: | :---: | :--- |
| **WMS** | Entrada Manual | **SÍ** (Incrementa) | `ENTRADA` | Sí | Crea registro en `Existencia` si no existía previamente. |
| **WMS** | Salida Manual | **SÍ** (Disminuye) | `SALIDA` | Sí | **Bloquea si el stock actual < cantidad solicitada** (No permite negativos). |
| **WMS** | Ajuste Físico | **SÍ** (Sobrescribe) | `AJUSTE` | Sí | Reemplaza el stock anterior directamente por el valor real contado. |
| **COMPRAS**| Registrar/Autorizar OC | NO | Ninguno | Sí | Bloquea la edición del documento de forma permanente tras autorizarse. |
| **COMPRAS**| Registrar Recepción | **SÍ** (Incrementa) | `ENTRADA` | Sí | Genera folio de serie activa; permite recepciones parciales de OC/OP. |
| **PRODUCCIÓN**| Confirmar OP | **SÍ** (Insumos disminuyen) | `SALIDA` | Sí | **Busca el BOM activo y bloquea confirmación** si falta un solo insumo en stock. |
| **PRODUCCIÓN**| Entrada Producto Terminado | **SÍ** (Incrementa PT) | `ENTRADA` | Sí | Canalizado por Recepciones; **asocia el movimiento físico al ID de la OP**. |
| **VENTAS**| Crear Cotización | NO | Ninguno | No | No compromete stock de manera especulativa en fase de negociación. |
| **VENTAS**| Autorizar y Convertir Pedido | **SÍ** (Disminuye PT) | `SALIDA` | Sí | **Bloquea la venta** si el stock en el almacén por talla es insuficiente. |
| **VENTAS**| Modificar Pedido Autorizado | **SÍ** (Suma o Resta) | `ENTRADA` o `SALIDA` | Sí | **Calcula el Delta:** si reduce pide menos devuelve stock, si aumenta descuenta. |
| **CxC** | Registrar Factura Directa | NO | Ninguno | Sí | **Atómica:** crea Factura, Cuenta por Cobrar y Póliza Contable en un solo paso. |
| **CxC** | Facturar Pedido | NO | Ninguno | Sí | **Valida duplicidad:** impide emitir más de una factura activa por pedido. |

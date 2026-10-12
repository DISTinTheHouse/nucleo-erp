<div align="center">
    <p>
        <img src="../assets/dots.png" width="120">
    </p>
        <h1>Flujo de finanzas</h1>
        <h3>La app administra bancos, movimientos de dinero, documentos contables y saldos de clientes y proveedores por empresa.</h3>

  <p>
    <a href="#Bancos">Bancos</a> ·
    <a href="#CuentasBancarias">Cuentas Bancarias</a> ·
    <a href="#Conciliaciones">Conciliaciones</a> ·
    <a href="#PolizasContables">Polizas Contables</a> ·
    <a href="#NotasCredito">Notas de credito</a> ·
    <a href="#FacturasProveedor">Facturas de proveedor</a> ·
    <a href="#ProcesoFacturacion">Proceso de facturacion</a> ·
    <a href="#Cxcp">Cuentas por cobrar y cuentas por pagar</a> ·
  </p>
</div>


<a id="Bancos"></a>

## 1. Bancos

**Tablas:** `empresas`, `bancos`, `cuentas_bancarias`.

Registrar la institución bancaria con nombre, código, SWIFT y observaciones. Después puede consultarse, actualizarse o desactivarse. El banco sirve para asociar cuentas bancarias; su registro no modifica saldos. No puede eliminarse si tiene cuentas relacionadas.

```mermaid
flowchart TD
    A[Registrar banco] --> B[Consultar o actualizar datos]
    B --> C[Asociar cuentas bancarias]
    B --> D[Desactivar banco]
```

**Relaciones de tablas del flujo:**

```mermaid
erDiagram
    empresas ||--o{ bancos : "empresa"
    empresas ||--o{ cuentas_bancarias : "empresa"
    bancos ||--o{ cuentas_bancarias : "banco"
```

<a id="CuentasBancarias"></a>
## 2. Cuentas bancarias

**Tablas:** `empresas`, `bancos`, `monedas`, `cuentas_bancarias`, `movimientos_bancarios`, `pagos`, `cobros`.

Seleccionar banco y moneda, capturar alias, titular, número de cuenta, CLABE y saldo de partida. La cuenta se utiliza para registrar cobros, pagos y movimientos adicionales.

Los abonos aumentan el saldo y los cargos lo disminuyen. Se puede consultar el saldo actual y los movimientos recientes. Para corregir un movimiento manual, cancelarlo y registrar el correcto; los movimientos de pagos o cobros se cancelan desde su documento de origen.

```mermaid
flowchart TD
    A[Seleccionar banco y moneda] --> B[Registrar cuenta y saldo de partida]
    B --> C{Operación}
    C -->|Cobro o abono| D[Aumentar saldo]
    C -->|Pago o cargo| E[Disminuir saldo]
    D --> F[Consultar saldo y movimientos]
    E --> F
    F --> G[Preparar conciliación]
```

**Relaciones de tablas del flujo:**

```mermaid
erDiagram
    empresas ||--o{ bancos : "empresa"
    empresas ||--o{ cuentas_bancarias : "empresa"
    bancos ||--o{ cuentas_bancarias : "banco"
    monedas ||--o{ cuentas_bancarias : "moneda"
    empresas ||--o{ cobros : "empresa"
    cuentas_bancarias ||--o{ cobros : "cuenta bancaria"
    empresas ||--o{ pagos : "empresa"
    cuentas_bancarias ||--o{ pagos : "cuenta bancaria"
    cuentas_bancarias ||--o{ movimientos_bancarios : "cuenta bancaria"
    pagos |o--o{ movimientos_bancarios : "pago"
    cobros |o--o{ movimientos_bancarios : "cobro"
```

<a id="Conciliaciones"></a>

## 3. Conciliaciones

**Tablas:** `cuentas_bancarias`, `movimientos_bancarios`, `conciliaciones_bancarias`, `conciliacion_detalle`, `pagos`, `cobros`.

1. Seleccionar cuenta, período y saldo del estado de cuenta bancario.
2. Preparar la conciliación: se reúnen los movimientos no cancelados del período, se calcula el saldo interno al cierre y se muestra la diferencia frente al banco.
3. Revisar las diferencias y corregir las operaciones que corresponda. Preparar nuevamente actualiza el borrador del mismo período.
4. Cerrar cuando los saldos coincidan, con una tolerancia de un centavo. La conciliación queda **Cerrada** y sus movimientos **Conciliados**.

El cierre no modifica el saldo bancario. Cancelar la conciliación cambia su estado a **Cancelada**, sin revertir saldos ni desmarcar sus movimientos.

```mermaid
flowchart TD
    A[Elegir cuenta, período y saldo del banco] --> B[Preparar o actualizar borrador]
    B --> C[Comparar saldo interno con saldo del banco]
    C --> D{Diferencia dentro de un centavo}
    D -->|No| E[Revisar y corregir operaciones]
    E --> B
    D -->|Sí| F[Cerrar conciliación]
    F --> G[Marcar movimientos como conciliados]
    B --> H[Cancelar conciliación]
    F --> H
    H --> I[Conservar saldos y marcas de movimientos]
```

**Relaciones de tablas del flujo:**

```mermaid
erDiagram
    cuentas_bancarias ||--o{ cobros : "cuenta bancaria"
    cuentas_bancarias ||--o{ pagos : "cuenta bancaria"
    cuentas_bancarias ||--o{ movimientos_bancarios : "cuenta bancaria"
    pagos |o--o{ movimientos_bancarios : "pago"
    cobros |o--o{ movimientos_bancarios : "cobro"
    cuentas_bancarias ||--o{ conciliaciones_bancarias : "cuenta bancaria"
    conciliaciones_bancarias ||--o{ conciliacion_detalle : "conciliacion"
    movimientos_bancarios ||--o{ conciliacion_detalle : "movimiento bancario"
```
<a id="PolizasContables"></a>

## 4. Pólizas contables

**Tablas:** `empresas`, `sucursales`, `usuarios`, `polizas`, `poliza_detalle`, `cuentas_contables`, `centros_costo`, `facturas`, `facturas_proveedor`, `pagos`, `cobros`, `movimientos_bancarios`.

Registrar una póliza de **Diario**, **Ingreso**, **Egreso** o **Ajuste**, indicando sucursal, concepto y partidas con sus cuentas contables, cargos y abonos. Las partidas pueden vincularse a facturas, pagos, cobros o movimientos bancarios.

La póliza inicia en **Borrador**. Validar que los cargos y abonos coincidan y contabilizarla para dejarla **Contabilizada**. Puede cancelarse; una póliza cancelada no puede contabilizarse y una contabilizada debe cancelarse antes de eliminarse.

```mermaid
flowchart TD
    A[Capturar concepto y partidas] --> B[Póliza en borrador]
    B --> C[Validar cargos y abonos]
    C --> D{Cuadre correcto}
    D -->|No| E[Revisar importes]
    E --> C
    D -->|Sí| F[Contabilizar]
    B --> G[Cancelar]
    F --> G
    G --> H[Póliza cancelada]
```

**Relaciones de tablas del flujo:**

```mermaid
erDiagram
    empresas ||--o{ cuentas_contables : "empresa"
    cuentas_contables |o--o{ cuentas_contables : "cuenta padre"
    empresas ||--o{ centros_costo : "empresa"
    empresas ||--o{ polizas : "empresa"
    sucursales ||--o{ polizas : "sucursal"
    centros_costo |o--o{ polizas : "centro costo"
    usuarios |o--o{ polizas : "usuario creacion"
    polizas |o--o{ poliza_detalle : "poliza"
    cuentas_contables |o--o{ poliza_detalle : "cuenta contable"
    centros_costo |o--o{ poliza_detalle : "centro costo"
    facturas |o--o{ poliza_detalle : "factura"
    facturas_proveedor |o--o{ poliza_detalle : "factura proveedor"
    pagos |o--o{ poliza_detalle : "pago"
    cobros |o--o{ poliza_detalle : "cobro"
    movimientos_bancarios |o--o{ poliza_detalle : "movimiento bancario"
    empresas ||--o{ facturas : "empresa"
    sucursales ||--o{ facturas : "sucursal"
    empresas ||--o{ facturas_proveedor : "empresa"
    sucursales ||--o{ facturas_proveedor : "sucursal"
    empresas ||--o{ cobros : "empresa"
    empresas ||--o{ pagos : "empresa"
    pagos |o--o{ movimientos_bancarios : "pago"
    cobros |o--o{ movimientos_bancarios : "cobro"
```

<a id="NotasCredito"></a>

## 5. Notas de crédito

**Tablas:** `clientes`, `facturas`, `factura_detalle`, `notas_creditos`, `nota_credito_detalle`, `cuentas_por_cobrar`.

Seleccionar la factura y su cliente, capturar motivo, importes y partidas relacionadas. La nota puede guardarse en **Borrador** y después pasar a **Emitida**, o registrarse directamente emitida.

Al emitirse, reduce el saldo de la CxC de la factura. Debe existir esa cuenta y el crédito no puede superar su saldo. La cuenta queda **Parcial** o **Pagada** según el saldo restante. Cancelar una nota emitida devuelve el importe acreditado; una nota cancelada no puede editarse y una emitida debe cancelarse antes de eliminarse.

```mermaid
flowchart TD
    A[Seleccionar factura y cliente] --> B[Capturar motivo, partidas e importes]
    B --> C[Borrador o emisión directa]
    C --> D{Existe CxC y el crédito no supera el saldo}
    D -->|No| E[Revisar cuenta e importe]
    E --> C
    D -->|Sí| F[Emitir nota y reducir saldo de CxC]
    F --> G[CxC parcial o pagada]
    F --> H[Cancelar nota emitida]
    H --> I[Devolver importe a CxC]
    I --> J[Nota cancelada]
```

**Relaciones de tablas del flujo:**

```mermaid
erDiagram
    clientes ||--o{ facturas : "cliente"
    facturas ||--o{ factura_detalle : "factura"
    clientes ||--o{ cuentas_por_cobrar : "cliente"
    facturas ||--o{ cuentas_por_cobrar : "factura"
    facturas ||--o{ notas_creditos : "factura"
    clientes ||--o{ notas_creditos : "cliente"
    notas_creditos ||--o{ nota_credito_detalle : "nota credito"
    factura_detalle ||--o{ nota_credito_detalle : "factura detalle"
```
<a id="Pagos"></a>

## 6. Pagos

**Tablas:** `empresas`, `proveedores`, `cuentas_bancarias`, `pagos`, `pago_detalle`, `cuentas_por_pagar`, `facturas_proveedor`, `movimientos_bancarios`.

Registrar proveedor, cuenta bancaria, fecha, método, referencia y los importes destinados a las CxP. Puede guardarse en **Borrador** o directamente **Aplicado**.

Al aplicar, debe haber al menos una obligación, cada importe debe ser positivo y no superar su saldo, y las aplicaciones deben coincidir con el total del pago. Las facturas de proveedor deben estar **Registradas**. El pago reduce las CxP, disminuye el saldo bancario y genera un cargo pendiente de conciliación.

Cancelar devuelve los importes a las CxP, restituye el saldo bancario y cancela el movimiento asociado.

```mermaid
flowchart TD
    A[Capturar proveedor, cuenta e importes por CxP] --> B[Borrador o aplicación directa]
    B --> C{Importes válidos y facturas registradas}
    C -->|No| D[Revisar aplicaciones y facturas]
    D --> B
    C -->|Sí| E[Aplicar pago y reducir CxP]
    E --> F[Disminuir saldo bancario y generar cargo]
    F --> G[Conciliar movimiento]
    F --> H[Cancelar pago aplicado]
    H --> I[Restituir saldos de CxP y banco]
    I --> J[Cancelar movimiento asociado]
```

**Relaciones de tablas del flujo:**

```mermaid
erDiagram
    empresas ||--o{ cuentas_bancarias : "empresa"
    empresas ||--o{ facturas_proveedor : "empresa"
    proveedores ||--o{ facturas_proveedor : "proveedor"
    empresas ||--o{ cuentas_por_pagar : "empresa"
    proveedores ||--o{ cuentas_por_pagar : "proveedor"
    facturas_proveedor ||--o| cuentas_por_pagar : "factura proveedor"
    empresas ||--o{ pagos : "empresa"
    proveedores ||--o{ pagos : "proveedor"
    cuentas_bancarias ||--o{ pagos : "cuenta bancaria"
    pagos ||--o{ pago_detalle : "pago"
    cuentas_por_pagar ||--o{ pago_detalle : "cxp"
    cuentas_bancarias ||--o{ movimientos_bancarios : "cuenta bancaria"
    pagos |o--o{ movimientos_bancarios : "pago"
```

<a id="FacturasProveedor"></a>

## 7. Facturas de proveedor

**Tablas:** `empresas`, `sucursales`, `proveedores`, `monedas`, `ordenes_compra`, `orden_compra_detalle`, `recepciones`, `recepcion_detalle`, `productos`, `facturas_proveedor`, `factura_proveedor_detalle`, `cuentas_por_pagar`.

Capturar proveedor, sucursal, orden de compra, recepción, moneda, folio, vencimiento e importes. Las partidas corresponden a los productos de la orden y la recepción. Se puede adjuntar la factura en PDF y obtener un documento que la reúne con la orden de compra y la recepción.

Al pasar de **Borrador** a **Registrada**, se genera una CxP pendiente por el total. Mientras exista una CxP vigente, se restringen cambios de total, proveedor y moneda, así como el regreso a borrador o la cancelación. Una factura cancelada no puede modificarse ni reactivarse.

```mermaid
flowchart TD
    A[Orden de compra y recepción] --> B[Capturar factura, partidas e importes]
    B --> C[Borrador o registro directo]
    C --> D[Factura registrada]
    D --> E[Generar CxP pendiente por el total]
    E --> F[Aplicar pagos]
    B --> G[Adjuntar PDF]
    G --> H[Obtener PDF con orden y recepción]
```

**Relaciones de tablas del flujo:**

```mermaid
erDiagram
    empresas ||--o{ facturas_proveedor : "empresa"
    sucursales ||--o{ facturas_proveedor : "sucursal"
    proveedores ||--o{ facturas_proveedor : "proveedor"
    ordenes_compra ||--o{ facturas_proveedor : "oc"
    recepciones ||--o{ facturas_proveedor : "recepcion"
    monedas ||--o{ facturas_proveedor : "moneda"
    facturas_proveedor ||--o{ factura_proveedor_detalle : "factura proveedor"
    orden_compra_detalle ||--o{ factura_proveedor_detalle : "oc detalle"
    recepcion_detalle ||--o{ factura_proveedor_detalle : "recepcion detalle"
    productos ||--o{ factura_proveedor_detalle : "producto"
    empresas ||--o{ cuentas_por_pagar : "empresa"
    proveedores ||--o{ cuentas_por_pagar : "proveedor"
    facturas_proveedor ||--o| cuentas_por_pagar : "factura proveedor"
```

<a id="ProcesoFacturacion"></a>

## 8. Proceso de facturación

**Tablas:** `empresas`, `sucursales`, `clientes`, `monedas`, `pedidos`, `pedido_detalle`, `productos`, `series_folios`, `facturas`, `factura_detalle`, `cuentas_por_cobrar`, `polizas`, `poliza_detalle`, `cuentas_contables`, `centros_costo`.

- **Desde un pedido:** crear una factura en borrador con cliente, moneda, productos, cantidades y precios del pedido. Se asigna folio y se calculan totales; esta ruta calcula descuentos e impuestos en cero. No admite otra factura activa no cancelada para el mismo pedido.
- **Pendiente de cobro:** capturar cliente, moneda, folio, vencimiento e importes, con pedido opcional. Se genera una factura **Emitida**, una CxC **Pendiente** por el total y una póliza de ingreso en **Borrador**. Requiere cuentas contables y un centro de costo activo.
- **Comprobante fiscal:** el proyecto permite emitir CFDI mediante Facturama y obtener el PDF, XML y acuse correspondiente. El registro de la factura y su saldo pendiente se realiza mediante el flujo anterior.

```mermaid
flowchart TD
    A{Ruta de facturación} -->|Desde pedido| B[Verificar que no tenga factura activa]
    B --> C[Crear factura en borrador con partidas y totales]
    A -->|Pendiente de cobro| D[Capturar cliente, moneda, vencimiento e importes]
    D --> E[Validar cuentas contables y centro de costo]
    E --> F[Crear factura emitida]
    F --> G[Crear CxC pendiente y póliza en borrador]
    G --> H[Gestionar cobros]
    A -->|Comprobante fiscal| I[Emitir CFDI con Facturama]
    I --> J[Obtener PDF, XML y acuse]
```

**Relaciones de tablas del flujo:**

```mermaid
erDiagram
    empresas ||--o{ cuentas_contables : "empresa"
    cuentas_contables |o--o{ cuentas_contables : "cuenta padre"
    empresas ||--o{ centros_costo : "empresa"
    empresas ||--o{ polizas : "empresa"
    sucursales ||--o{ polizas : "sucursal"
    centros_costo |o--o{ polizas : "centro costo"
    polizas |o--o{ poliza_detalle : "poliza"
    cuentas_contables |o--o{ poliza_detalle : "cuenta contable"
    centros_costo |o--o{ poliza_detalle : "centro costo"
    facturas |o--o{ poliza_detalle : "factura"
    empresas ||--o{ facturas : "empresa"
    sucursales ||--o{ facturas : "sucursal"
    clientes ||--o{ facturas : "cliente"
    pedidos |o--o{ facturas : "pedido"
    series_folios |o--o{ facturas : "serie folio"
    monedas ||--o{ facturas : "moneda"
    facturas ||--o{ factura_detalle : "factura"
    pedido_detalle ||--o{ factura_detalle : "pedido detalle"
    productos ||--o{ factura_detalle : "producto"
    empresas |o--o{ cuentas_por_cobrar : "empresa"
    clientes ||--o{ cuentas_por_cobrar : "cliente"
    facturas ||--o{ cuentas_por_cobrar : "factura"
```

<a id="Cxcp"></a>

## 9. Cuentas por pagar y cuentas por cobrar (CxP y CxC)

**Tablas:** `empresas`, `clientes`, `proveedores`, `facturas`, `facturas_proveedor`, `cuentas_por_cobrar`, `cuentas_por_pagar`, `cuentas_bancarias`, `cobros`, `cobro_detalle`, `pagos`, `pago_detalle`, `movimientos_bancarios`, `notas_creditos`, `alertas_mora`.

**CxP:** nace al registrar la factura de proveedor, con saldo inicial igual al total y su vencimiento. Existe una sola cuenta por factura. Los pagos reducen el saldo y la dejan **Parcial** o **Pagada**. Con pagos aplicados, se bloquean cambios de los datos que afectan la obligación y su eliminación.

**CxC:** se genera al registrar una factura pendiente de cobro; también puede registrarse asociada a una factura. Los cobros y notas de crédito reducen su saldo. Para cobrar, indicar cliente, cuenta bancaria e importes aplicados: deben ser positivos, no superar los saldos y coincidir con el total. El cobro aumenta el saldo bancario y genera un abono; su cancelación revierte estos efectos.

Ambas cuentas permiten consultar obligaciones, saldos y vencimientos. Sus estados son **Pendiente**, **Parcial**, **Pagada** y **Cancelada**. Una cuenta se considera vencida cuando conserva saldo después de su vencimiento. Se pueden generar alertas de mora: leve de 1 a 7 días, moderada de 8 a 30, grave de 31 a 60 y crítica a partir de 61.

```mermaid
flowchart TD
    A[Factura de proveedor registrada] --> B[CxP pendiente]
    B --> C[Aplicar pago]
    D[Factura pendiente de cobro] --> E[CxC pendiente]
    E --> F[Aplicar cobro o nota de crédito]
    C --> G[Reducir saldo de la obligación] 
    F --> G
    G --> H{Saldo restante}
    H -->|Mayor que cero| I[Cuenta parcial]
    H -->|Cero| J[Cuenta pagada]
    B --> K[Consultar vencimiento y saldo]
    E --> K
    I --> K
    K --> L{Saldo pendiente y fecha vencida}
    L -->|Sí| M[Generar alerta de mora]
```

**Relaciones de tablas del flujo:**

```mermaid
erDiagram
    empresas ||--o{ cuentas_bancarias : "empresa"
    empresas ||--o{ facturas : "empresa"
    clientes ||--o{ facturas : "cliente"
    empresas ||--o{ facturas_proveedor : "empresa"
    proveedores ||--o{ facturas_proveedor : "proveedor"
    empresas |o--o{ cuentas_por_cobrar : "empresa"
    clientes ||--o{ cuentas_por_cobrar : "cliente"
    facturas ||--o{ cuentas_por_cobrar : "factura"
    empresas ||--o{ cuentas_por_pagar : "empresa"
    proveedores ||--o{ cuentas_por_pagar : "proveedor"
    facturas_proveedor ||--o| cuentas_por_pagar : "factura proveedor"
    empresas ||--o{ cobros : "empresa"
    clientes ||--o{ cobros : "cliente"
    cuentas_bancarias ||--o{ cobros : "cuenta bancaria"
    cobros ||--o{ cobro_detalle : "cobro"
    cuentas_por_cobrar ||--o{ cobro_detalle : "cxc"
    empresas ||--o{ pagos : "empresa"
    proveedores ||--o{ pagos : "proveedor"
    cuentas_bancarias ||--o{ pagos : "cuenta bancaria"
    pagos ||--o{ pago_detalle : "pago"
    cuentas_por_pagar ||--o{ pago_detalle : "cxp"
    cuentas_bancarias ||--o{ movimientos_bancarios : "cuenta bancaria"
    pagos |o--o{ movimientos_bancarios : "pago"
    cobros |o--o{ movimientos_bancarios : "cobro"
    facturas ||--o{ notas_creditos : "factura"
    clientes ||--o{ notas_creditos : "cliente"
    empresas ||--o{ alertas_mora : "empresa"
    cuentas_por_cobrar |o--o{ alertas_mora : "cuenta por cobrar"
    cuentas_por_pagar |o--o{ alertas_mora : "cuenta por pagar"
```


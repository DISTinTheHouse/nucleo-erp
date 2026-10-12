# Plan de pólizas automáticas y contrapóliza: checklist de avance

Objetivo: trazabilidad financiera y contable. Todo movimiento genera su póliza automáticamente (nadie las captura a mano) y cada cancelación genera una contrapóliza fechada el mismo día.

Marca `[x]` conforme se complete cada punto. Contexto, decisiones y detalle técnico: `DOCS/api/DOCUMENTACION_API.md` (sección "Pólizas automáticas — avance") y `DOCS/arquitectura/mapa-polizas.html`.

## Cómo se contabiliza cada movimiento

| Movimiento | Cargo | Abono |
|---|---|---|
| Factura de venta | Clientes (total) | Ventas (neto) + IVA trasladado |
| Cobro | Bancos | Clientes |
| Nota de crédito | Devoluciones y rebajas (neto) + IVA trasladado | Clientes |
| Factura de proveedor | Compras/Inventario o Gasto + IVA acreditable | Proveedores |
| Pago | Proveedores | Bancos |
| Movimiento bancario sin documento | Bancos / contrapartida | contrapartida / Bancos |
| Factura parcial | igual que factura, solo por la parcialidad | |

**Contrapóliza:** misma póliza con cargos y abonos invertidos, ligada con `poliza_reversa_de`, concepto "Cancelación de …", fecha = día de la cancelación. El original no se edita ni se borra.

## Reglas de la junta con finanzas
- [ ] No permitir cobrar/pagar una factura cancelada
- [ ] No permitir pagar una factura más de una vez (pagos parciales siguen permitidos: por confirmar)
- [ ] Un pago (y un cobro) genera una póliza
- [ ] Todos los movimientos del diagrama generan póliza
- [ ] Cancelar genera otra póliza con los movimientos contrarios, el mismo día
- [ ] Una factura con cobros aplicados se puede cancelar
- [ ] Notas de crédito incluidas
- [ ] Facturas PPD llevan complemento de pago
- [ ] Alertas de vencidas a 30 / 60 / 90 / 120 días, según el plazo de cada cliente

## F0 Cimientos y bugs
- [x] Constraint cargo XOR abono dentro de `Meta` en `PolizaDetalle` + migración 0028 (con verificación previa de datos)
- [x] Cobro/Pago: arreglar cancelación por PATCH y `/cancelar/` sobre Borrador (y DELETE de aplicados, que daba 500)
- [x] Cobro: no cobrar CxC/factura cancelada; no reaplicar un cobro/pago (transiciones Borrador→Aplicado→Cancelado)
- [x] Cerrar edición de saldo/estatus/total de CxC y quitar el alta manual (POST)
- [ ] Sucursal en Cobro/Pago/CuentaBancaria: se deriva de la factura/CxC en F1/F3 (pospuesto)

## F1 Motor
- [x] Campos de origen en `Poliza` (`origen_tipo`/`origen_id`) + unicidad (migración 0029)
- [x] `PolizaMotorService.generar()` reutilizando `PlanContableService` y `generate_poliza_folio`
- [x] `PolizaService.contrapoliza()` y `cancelar()` (la original queda Cancelada)
- [x] Respeta `ParametrosContabilidad` (interruptor y fecha de inicio)
- [x] Listado de pólizas sin consultas por renglón

Detalle: `DOCS/finanzas/motor-polizas.md`.

## F2 Factura de venta
- [x] Póliza nace Contabilizada con plan contable y folio atómico (`FacturaPolizaService`)
- [x] `GET /facturas/{id}/previsualizar-poliza/`

## F3 Cobro y pago
- [x] Póliza de cobro y su contrapóliza al cancelar
- [x] Póliza de pago y su contrapóliza al cancelar
- [ ] Conversión de moneda: hoy una cuenta bancaria en moneda distinta de la base se asienta al mismo importe

## F4 Otros documentos
- [ ] Nota de crédito (y su cancelación)
- [ ] Factura de proveedor
- [ ] Movimiento bancario

## F5 Cancelación de factura
- [ ] Acción `cancelar` en facturas (estatus, CxC, contrapóliza mismo día)
- [ ] Guardar UUID del CFDI y cancelarlo en Facturama

## F6 Factura parcial
- [ ] Emisión por parcialidad con CxC y póliza

## F7 Alertas
- [ ] Días de crédito por cliente
- [ ] Umbrales 30/60/90/120
- [ ] Evitar alertas duplicadas al correr sin `--empresa`

## F8 PPD
- [ ] IVA por trasladar/acreditar (cuentas y conceptos nuevos)
- [ ] `metodo_pago` en `Factura`
- [ ] Complemento de pago (REP)

## F9 Trazabilidad y reportes
- [ ] Filtro `?origen=` y endpoint de rastro por documento
- [ ] Libro diario, auxiliar y balanza de comprobación

## Preguntas abiertas para contabilidad
- [ ] Factura cancelada con cobros: ¿saldo a favor del cliente o se cancelan también los cobros?
- [ ] ¿"Pagar más de una vez" prohíbe parcialidades?
- [ ] IVA en PPD: cuentas por trasladar/acreditar
- [ ] Compras: ¿inventario o gasto por defecto?
- [ ] Descuento sobre ventas: ¿cuenta aparte (4160) o neto en Ventas?

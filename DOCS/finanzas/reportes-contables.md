# Reportes contables

Sobre las pólizas. Base: `/api/v1/finanzas/reportes-contables/`. Todos son `GET` y piden `fecha_inicio` y `fecha_fin` (`AAAA-MM-DD`).

- Cuentan las pólizas **Contabilizadas** y las **Canceladas** (la contrapóliza las neutraliza). Los Borradores no cuentan.
- Cada empresa ve lo suyo. El superusuario debe mandar `?empresa=ID`.
- Los importes son texto con 2 decimales. No hay conversión de moneda.

| Reporte | Ruta | Qué trae |
|---|---|---|
| Libro diario | `/libro-diario/` | Renglones por fecha y póliza. Filtros: `sucursal`, `tipo`. Pagina con `limit` (200 por omisión, máximo 1000) y `offset`. |
| Auxiliar | `/auxiliar/?cuenta=ID` | Movimientos de una cuenta con saldo corrido, saldo inicial y final. Pagina igual. |
| Balanza | `/balanza/` | Por cuenta: saldo inicial, cargos, abonos y saldo final. Sin paginar. |

## Libro diario
`total_cargos` y `total_abonos` son de todo el periodo, no de la página. Cada renglón trae `poliza_id`, `fecha`, `folio`, `tipo`, `concepto`, `estatus`, `origen_tipo`, `origen_id`, `contrapoliza_de`, `cuenta_id`, `cuenta_codigo`, `cuenta_nombre`, `cargo`, `abono`, `referencia`.

## Auxiliar
`cuenta` es obligatoria y debe ser de la empresa. Trae `cuenta` (con `naturaleza`), `saldo_inicial`, `total_cargos`, `total_abonos`, `saldo_final` y `movimientos` con `saldo` en cada uno.

## Balanza
`cuadre_correcto` es `true` si todo lo contabilizado hasta `fecha_fin` tiene cargos iguales a abonos.

## Naturaleza de la cuenta
Activo, Gasto y Costo son **deudoras** (saldo = cargos - abonos). Pasivo, Capital e Ingreso son **acreedoras** (saldo = abonos - cargos).

## Rastro de un documento
`GET /api/v1/finanzas/polizas/?origen_tipo=Factura&origen_id=15` regresa la póliza del documento y, si se canceló, su contrapóliza (`poliza_reversa_de` apunta a la original).

Valores de `origen_tipo`: `Factura`, `Cobro`, `Pago`, `NotaCredito`, `FacturaProveedor`, `MovimientoBancario`.

## Rendimiento
Todo se calcula en la base con agregados: el número de consultas no depende del número de pólizas.

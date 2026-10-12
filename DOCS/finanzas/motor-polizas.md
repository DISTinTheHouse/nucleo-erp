# Motor de pólizas

Genera la póliza de un documento y la cancela con una contrapóliza.

## Generar

`PolizaMotorService.generar(...)` en `finanzas/services/poliza_motor_service.py`.

- Cada documento manda sus líneas con conceptos contables (`ConceptoContable`). El motor busca la cuenta de cada concepto en `ConfiguracionContable`.
- No genera nada (regresa `None`) si `ParametrosContabilidad.contabilizacion_automatica` está apagada o si el documento es anterior a `fecha_inicio_contabilizacion`.
- Un documento tiene una sola póliza. Si ya existe, regresa esa (`uq_poliza_origen` sobre `origen_tipo` + `origen_id`).
- La póliza nace **Contabilizada** y no se edita.
- Si falta alguna cuenta, falla y lista todos los conceptos faltantes juntos. No queda nada guardado.
- Si cargos y abonos no cuadran (tolerancia 0.01), falla y no guarda.
- Los renglones en ceros se omiten.
- Costo por póliza: 1 consulta para las cuentas, 1 para el centro de costo, el folio y 1 `bulk_create`.

## Cancelar

`PolizaService.cancelar(poliza, usuario)`.

| Póliza | Qué pasa |
|---|---|
| Borrador | Pasa a Cancelada. No genera contrapóliza. |
| Contabilizada | Se crea la contrapóliza y la original pasa a Cancelada. |
| Cancelada | No hace nada. Regresa la contrapóliza que ya existe. |

La contrapóliza invierte cargos y abonos, copia las FK del documento, lleva la fecha de hoy y queda ligada con `poliza_reversa_de`.

API: `POST /api/v1/finanzas/polizas/{id}/cancelar/` responde la póliza original con `contrapoliza_id`.

## Listado

`GET /polizas/` lee todas las líneas en una consulta (`prefetch_related`). Los totales se calculan en memoria. Las consultas no crecen con el número de pólizas.

# Alertas de mora

Una alerta por cuenta por cobrar o por pagar vencida, en los hitos de 30, 60, 90 y 120 días.

| Días vencida | Nivel |
|---|---|
| menos de 30 | sin alerta |
| 30 a 59 | Leve |
| 60 a 89 | Moderada |
| 90 a 119 | Grave |
| 120 o más | Crítica |

Los hitos están en `HITOS_MORA` (`finanzas/services/alerta_mora_service.py`).

## Plazo del cliente
Los días vencidos se cuentan desde `fecha_vencimiento`. Al crear una factura (`registrar-pendiente-cobro`) sin `fecha_vencimiento`, vence a `fecha_emision + Cliente.dias_credito`. Si viene `fecha_vencimiento`, esa manda. Sin ninguna de las dos, la cuenta no tiene vencimiento y no alerta.

## Cómo se generan
- `POST /api/v1/finanzas/alertas-mora/generar/` o `python manage.py generar_alertas_mora [--empresa ID]`.
- Cada corrida **sincroniza**: crea las nuevas, actualiza `dias_mora` y `nivel` de las existentes y borra las de cuentas ya pagadas, canceladas o sin mora. Con o sin `--empresa` no duplica.
- `notificado` se conserva. Si el nivel sube (por ejemplo de Leve a Moderada) vuelve a `false` para avisar de nuevo.
- La base impide dos alertas para la misma cuenta (`uq_alerta_mora_cxc` y `uq_alerta_mora_cxp`). La migración 0030 deja la alerta más reciente de cada cuenta si ya había repetidas.
- **No hay scheduler.** Para que corra a diario hay que programar el comando (cron o Vercel Cron).

## Rendimiento
Una consulta por tipo de cuenta (sin cargar modelos), una de alertas existentes, y `bulk_create`, `bulk_update` y un `delete` por lote. Las consultas no crecen con el número de cuentas.

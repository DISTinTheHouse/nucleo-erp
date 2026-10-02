# Envío (logística)

`Envio` ya tiene API: `POST/GET /api/v1/logistica/envios/` (pedido + transportista, empresa/sucursal se derivan del pedido).

**Decisión:** no se activa la cadena `EnvioDetalle → Entrega → EntregaDetalle`. Es redundante — `Despacho → DespachoDetalle → PackingDetalle → PickingDetalle → PedidoDetalle` ya responde "qué se entregó de este pedido". Si algún día se necesita, se retoma, pero hoy duplicaría datos y los desincronizaría.

**Flujo:** se crea el Envío primero (pedido + transportista) → luego cada `Despacho` lo elige del catálogo que ya trae `DespachoService.onboarding_payload` (eso ya funcionaba, solo faltaba poder crear el Envío).

**Pendiente, no bloqueante:** `Transportista` no tiene campo `nombre` — el serializer de Despacho ya intenta leerlo pero siempre sale `null`.

# Plan RFID

## Objetivo

Definir una ruta clara y escalable para incorporar:

- [x] Impresion de etiquetas Zebra normales
- [x] Impresion de etiquetas RFID
- [x] Lectura de tags RFID
- [ ] Automatizacion de flujos WMS con RFID

La idea es implementarlo por fases, sin mezclar todo desde el inicio.

---

## Vision General

### Backend Django

- genera datos de etiquetas
- genera ZPL
- registra trazabilidad
- valida reglas de negocio
- guarda eventos RFID

### Frontend Next.js

- muestra interfaces de impresion y lectura
- selecciona impresora
- envia ZPL a Zebra Browser Print
- consume onboarding y endpoints de negocio

### Zebra Browser Print

- puente entre navegador e impresora Zebra instalada en la PC

### Impresora / Lector RFID

- imprime etiquetas
- graba tags RFID
- lee EPC/TID segun el dispositivo disponible

---

## Fase 1 - Imprimir Etiquetas Zebra Normales

### Objetivo

Poder imprimir etiquetas desde Next.js usando Zebra Browser Print.

### Alcance

- [ ] etiqueta de producto
- [ ] etiqueta de ubicacion
- [ ] etiqueta de picking
- [ ] etiqueta de caja o packing

### Flujo

- [x] frontend solicita datos o ZPL al backend
- [x] backend responde ZPL listo
- [x] frontend usa Browser Print
- [x] Zebra imprime localmente

### Entregables

- [x] endpoint para obtener ZPL
- [x] plantilla base de etiquetas
- [x] boton de impresion en frontend
- [x] validacion de impresora disponible

### Ejemplos de endpoints

- [ ] `GET /api/v1/wms/etiquetas/producto/{id}/zpl/`
- [ ] `GET /api/v1/wms/etiquetas/ubicacion/{id}/zpl/`
- [ ] `GET /api/v1/wms/etiquetas/picking/{id}/zpl/`

### Meta de esta fase

Resolver impresion real sin entrar todavia a RFID.

### Validacion QA completada

- [x] Se habilito `GET /QA/imprimir_etiqueta/` para pruebas locales con Zebra Browser Print.
- [x] La pantalla permite buscar por `sku`, `codigo`, `cod_proscai` y nombre.
- [x] Si no existen variantes, la impresion puede hacerse desde `Producto` usando codigo base.
- [x] Se valido impresion real de una etiqueta para `AMBASSADOR` con codigo `93E0`.
- [x] Se habilito el retorno directo a `GET /QA/rfid/recepciones/?encuadre={id}` para cerrar el ciclo de prueba.

### Aprendizajes de implementacion

- [x] En entorno local conviene servir Browser Print desde rutas QA dedicadas para no depender de `collectstatic`.
- [x] El Zebra `MC3300X` pudo participar en el flujo actual como lector de codigo de barras enviando el valor al input web.
- [x] Esta validacion confirma la fase de impresion y captura asistida, pero todavia no constituye lectura RFID real.

---

## Fase 2 - Imprimir Etiquetas RFID

### Objetivo

Grabar tags RFID y al mismo tiempo imprimir la etiqueta fisica.

### Alcance

- [x] definir estructura EPC
- [x] generar ZPL compatible con RFID
- [x] relacionar etiqueta impresa con producto o unidad logistica

### Decisiones necesarias

- que se grabara en el tag:
  - [x] EPC
  - [ ] SKU
  - [x] serie (serial consecutivo 4d en detalle)
  - [ ] lote
  - [ ] identificador interno
- que objetos del ERP tendran RFID:
  - [x] producto
  - [x] variante
  - [ ] caja
  - [ ] pallet
  - [ ] ubicacion

### Entregables

- [x] generador de ZPL RFID
- [x] estrategia de EPC
- [x] registro de etiqueta RFID emitida

### Modelo sugerido

- `EtiquetaRFID`
  - [x] epc
  - [ ] tid
  - [x] producto
  - [x] producto_variante
  - [x] serie
  - [ ] lote
  - [x] estatus
  - [x] fecha_impresion

### Meta de esta fase

Pasar de impresion normal a impresion con identidad RFID trazable.

### Validacion QA completada (Fase 2)

- [x] Workspace QA `/QA/imprimir_etiqueta/` con botón **"Guardar e imprimir etiqueta(s) RFID"** (SKU/variante).
- [x] Workspace QA `/QA/imprimir_orden_compra/<id>/` con botón **"Guardar y imprimir etiquetas RFID"** (renglones de OC, guarda por renglon, badge de guardado).
- [x] Backend `RFIDLabelService` (archivo `wms/services/rfid_label_service.py`):
  - `_generate_epc_list()`: 24 hex = 96 bits SGTIN Gen2 (prefix 8 + ts_chunk 4 + idx 4 + random 8).
  - `_build_zpl_rfid()`: `^RS8` + `^RFW,H,,,E^FD{EPC}^FS` (escribe en hex el EPC Gen2 de 96 bits). Corregido el 2026-11-05: la versión anterior (`^RB96,,,1` + `^RFW,E`) usaba el formato *por particiones*, no hex, y el chip se quedaba con su EPC de fábrica.
  - `store_impresion()`: transacción atómica, exige sucursal del usuario, crea `EtiquetaRFIDImpresion` + bulk `EtiquetaRFIDDetalle` (epc=UPPER, UNIQUE, estado IMPRESO).
  - `_resolve_context()`: pertenencia a empresa + sucursal default ligada al usuario.
- [x] Endpoints guardar impresión (archivo `QA/views.py`):
  - POST `/QA/imprimir_etiqueta/guardar/` (SKU/Producto) → función `qa_guardar_impresion_sku`
  - POST `/QA/imprimir_orden_compra/<detalle_id>/guardar/` (OC renglón) → función `qa_guardar_impresion_oc`
- [x] Modelos finales (archivo `wms/models.py`):
  - `EtiquetaRFIDImpresion` (folio `LAB-{id:06d}`, usuario, empresa, sucursal, producto/variante, cantidad, printer_name, ZPL enviado, status).
  - `EtiquetaRFIDDetalle` (FK impresion, epc UNIQUE 64 chars, barcode_value, serial 4d, estado PENDIENTE/IMPRESO/LEIDO/CANCELADO).
- [x] Validación usuario real: Impresión LAB-000012 crea renglón en `admin/wms/etiquetarfidimpresion/` y N renglones en `admin/wms/etiquetarfiddetalle/` (00001BD7EF5F000148CB3BF2 / SKU 3000703XCH / 0001 / Impreso).

---

## Fase 3 - Leer Tags RFID

### Objetivo

Poder registrar lecturas RFID dentro del ERP.

### Alcance

- [x] recibir EPC leido
- [x] identificar a que producto o unidad corresponde
- [x] registrar evento
- [x] mostrar lectura en frontend

### Flujo

1. lector o middleware detecta tag
2. frontend o servicio local envia EPC al backend
3. backend identifica el registro relacionado
4. backend guarda evento de lectura

### Estado actual

- [x] Ya existe una prueba funcional de lectura en `QA/rfid/recepciones/`, pero hoy resuelve por `sku`, `codigo` o `cod_proscai`.
- [x] Workspace dedicado `GET /QA/scanner_rfid/` para lectura RFID en vivo.
- [x] El endpoint de hardware `POST /QA/scanner_rfid/receive/` recibe webhook del lector FX. Hoy: `POST /api/v1/wms/etiquetas-rfid/scans/receive/` con token del `LectorRFID`.
- [x] El endpoint de polling `GET /QA/scanner_rfid/get/` devuelve scans + match cruzado con EtiquetaRFIDDetalle. Hoy: `GET /api/v1/wms/etiquetas-rfid/scans/`.
- [x] El endpoint `GET /QA/scanner_rfid/clear/` purga staging rfid_scans (Purge List).
- [x] Match en vivo: cada scan nuevo indica MATCH=SI con folio LAB-xxxx, SKU, talla, color, barcode, serial y estado; o MATCH=NO si EPC no está registrado.

### Entregables

- [x] endpoint para registrar lectura RFID
- [x] historial de lecturas
- [x] consulta de EPC
- [x] validaciones de tags no registrados

### Modelos sugeridos

- `RFIDLectura`
  - [x] epc
  - [ ] tid
  - [ ] dispositivo
  - [ ] ubicacion
  - [ ] evento
  - [x] fecha_lectura

- `RFIDDispositivo`
  - [ ] nombre
  - [ ] tipo
  - [x] ip (reader_ip)
  - [ ] activo

Nota: modelo actual en uso = `RfidScan` (archivo `wms/models.py`): epc + reader_ip + antenna + rssi + created_at.

### Meta de esta fase

Tener trazabilidad de lectura, aunque todavia no automatice procesos.

### Validacion QA completada (Fase 3)

- [x] `scanner_rfid_receive` (archivo `QA/views.py`):
  - Soporta payloads FX: lista simple u objetos con llaves `tagData/tags/events/eventList/data/items/reads/readEvents/tagReadEvents`.
  - `_extract_epc_raw()`: keys idHex/data/epc/tagID/tidHex/epcHex/hex + anidado `{epc:{idHex:...}}`.
  - `_extract_antenna_rssi()` + `_find_by_key_substr()`: extrae antenna/rssi con keys nuevas (ant, source, antenna_number, port_no, rssi_value, peak_rssi, signal_strength, peakRssiValue, rssiDbm, signalStrength) + fuzzy substring.
  - EPC normalizado (strip separadores, lower, min 8 chars).
  - Deduplicado por batch + `bulk_create(batch_size=200)`.
  - Logs body[:4096] para diagnosticar payloads FX desconocidos.
- [x] `scanner_rfid_get`: match robusto con `_epc_variants()` — lower/upper, strip ceros inicio/fin, slice 24 hex prefijo/sufijo para padding de 128 bits.
- [x] Frontend workspace: polling 2s, uniqueCount, tag nuevo por EPC, badge MATCH SI/NO, antenna, rssi, scan velocity.
- [x] Purge List: limpia staging y resetea lastMaxId local.

### PENDIENTES menores por confirmar con hardware

- [ ] Que antenna y rssi dejen de ser `-` (null) en admin `wms/rfidscan/` — depende de las keys reales que mande el FX; ya hay logger 4096 chars y fuzzy match listo.
- [ ] Confirmar MATCH=SI con una etiqueta impresa con el ZPL corregido `^RS8` + `^RFW,H,,,E` (todas las etiquetas hasta LAB-000039 salieron con el ZPL viejo y su chip trae el EPC de fábrica; hay que imprimir nuevas). Ver "Ligado lectura ↔ etiqueta" abajo.

### Ligado lectura ↔ etiqueta (2026-11-05)

Diagnóstico: de 1,199 EPCs leídos solo 1 coincidía con `etiquetas_rfid_detalle`, y ese se había insertado a mano. Había tres causas, y ninguna era de sincronización:

1. El ZPL no grababa nuestro EPC en el chip (`^RFW,E`): el lector veía el EPC de fábrica.
2. No había ningún `LectorRFID` dado de alta. Desde que el webhook exige token (2026-10-06), el FX recibe 401 y no ha llegado ninguna lectura real.
3. Los `3035…` (SGTIN-96) son tags ajenos (de fábrica o de proveedores). Es correcto que no hagan match.

Resuelto en backend:
- [x] ZPL corregido: `^RS8` + `^RFW,H,,,E^FD{EPC}^FS` (una sola escritura; se quitaron `^RB96` y `^HV1`, que no validaban nada).
- [x] `RfidScan.etiqueta` (FK a `EtiquetaRFIDDetalle`): cada lectura queda ligada a la etiqueta que imprimimos, de la misma empresa del lector. Los tags ajenos quedan sin etiqueta.
- [x] Se liga al recibir la lectura (`scans/receive/`) y, si la lectura llegó antes que el registro, al registrar la impresión.
- [x] Primera lectura: `EtiquetaRFIDDetalle` pasa a `LEIDO` y guarda `leida_at`.
- [x] EPC normalizado igual en ambos lados (hex en mayúsculas, sin separadores); tolera relleno a 128 bits y ceros a la izquierda.
- [x] `scans/` lee el match ya ligado: 1 consulta para las 50 lecturas (antes, 2 y variantes recalculadas en cada polling).
- [x] Migración `wms.0020`: lecturas existentes en mayúsculas y ligadas (LAB-000039 queda `LEIDO`).

Listo para el FX (2026-11-05, segunda revisión):
- [x] Autenticación **Basic** del IoT Connector: contraseña = token del lector, usuario libre. El HTTP POST del IoT Connector solo ofrece Basic o TLS; no admite headers propios ni Bearer. También sigue sirviendo `?token=` en la URL.
- [x] Parser: un evento suelto `{"data": {"idHex": …}, "type": "SIMPLE"}` ya no se descarta; también acepta lotes en arreglo y NDJSON. Solo se guardan EPCs en hex (heartbeats y eventos de gestión se ignoran).
- [x] Reimpresión desde el historial: `zpl_enviado` de impresiones viejas se entrega con el bloque de grabado corregido (al vuelo, sin tocar la BD; 0.21 ms por listado).
- [x] Admin: `RfidScan` muestra su etiqueta; una lectura capturada a mano se liga igual que las del lector. `EtiquetaRFIDDetalle` muestra `leida_at`.
- [x] Simulación punta a punta en producción con rollback: preview → lectura antes del registro → registro como el frontend → lote con tag ajeno → NDJSON + heartbeat → polling (1 consulta, 0.10–0.15 s) → detalle `LEIDO` → reimpresión de LAB-000039 con ZPL correcto.

Prueba en piso (pendiente):
- [ ] Admin → `wms/lectorrfid/`: alta del lector en **LAZZAR / Toneles** (misma empresa y sucursal donde se imprime) y copiar su token.
- [ ] FX (IoT Connector) → endpoint **HTTP POST**: URL `https://nucleo-erp.vercel.app/api/v1/wms/etiquetas-rfid/scans/receive/`, autenticación **Basic** con usuario libre (p. ej. `fx-toneles`) y contraseña = token. Si el firmware no ofrece Basic: la misma URL con `?token=<token>`. Siempre `https` (la URL vieja `/QA/scanner_rfid/receive/` ya no existe: 404).
- [ ] Imprimir una etiqueta **nueva** desde el ERP (ZPL corregido).
- [ ] Leerla con el FX: la pantalla Scanner RFID muestra ✓ con folio/SKU/talla y la etiqueta pasa a `LEIDO`.

---

## Fase 4 - Automatizar Flujos WMS con RFID

### Objetivo

Usar RFID para mover procesos del WMS con menos captura manual.

### Casos de uso

- [ ] recepcion automatica
- [ ] transferencias automatizadas
- [ ] picking asistido por RFID
- [ ] packing con validacion por lectura
- [ ] despacho con confirmacion RFID
- [ ] conteos ciclicos automatizados

### Integracion con WMS

- recepcion
- reservas
- picking
- packing
- despacho
- conteos

### Regla clave

RFID no debe reemplazar primero la operacion manual; debe entrar despues de que el flujo manual ya este estable.

### Meta de esta fase

Reducir errores, mejorar velocidad y aumentar trazabilidad en almacen.

---

## Orden Recomendado de Implementacion

1. [x] Impresion Zebra normal
2. [x] ZPL RFID + grabacion de tags
3. [x] Lectura RFID + registro de eventos
4. [ ] Automatizacion WMS por etapas

---

## Arquitectura Recomendada

### Corto plazo

- [x] Django genera ZPL
- [x] Next.js imprime con Browser Print
- [x] Zebra imprime localmente

### Mediano plazo

- [x] Django genera ZPL RFID
- [x] Next.js imprime y graba tags
- [x] backend registra etiqueta RFID emitida

### Largo plazo

- [x] lector RFID o middleware envia eventos al backend
- [ ] backend conecta lecturas con WMS

---

## Riesgos y Cuidados

- no depender del navegador sin Browser Print instalado
- no mezclar impresion y lectura en una sola fase
- definir EPC antes de imprimir masivamente
- no automatizar WMS con RFID sin tener trazabilidad base estable
- validar compatibilidad exacta del modelo de impresora Zebra
- [x] Escribir el EPC con `^RS8` + `^RFW,H,,,E` (hex, EPC de 96 bits). `^RFW,E` + `^RB96,,,1` **no sirve**: es el formato por particiones decimales y deja el EPC de fábrica en el chip (corregido 2026-11-05).
- [x] Extractor antenna/rssi con fuzzy find + logger 4096 chars para payloads FX variables

---

## MVP Recomendado

### MVP 1

- [x] imprimir etiqueta Zebra normal desde Next.js

### MVP 2

- [x] imprimir etiqueta RFID con EPC generado por backend

### MVP 3

- [x] registrar lectura de EPC y mostrar historial

### MVP 4

- [ ] usar RFID en picking o despacho

---

## Conclusion

La ruta correcta para este proyecto es:

1. [x] imprimir bien
2. [x] grabar RFID
3. [x] leer RFID
4. [ ] automatizar WMS

Ese orden reduce riesgo, facilita mantenimiento y permite avanzar con valor real desde la primera fase.

---

## Resumen CHECKBOX actualizado al commit `ace44de`

- **FASE 1 = 100% ✅** (Zebra normales, F1 validación QA, endpoints, MVPs, flujo).
- **FASE 2 = 100% ✅** (Estructura EPC 24 hex, ZPL RFID Gen2 `^RS8` + `^RFW,H,,,E` —corregido 2026-11-05—, registro impreso en DB con usuario/empresa/sucursal, 2 workspaces QA SKU+OC, MVPs).
- **FASE 3 = 95% ✅** (receive+get+clear implementados, match live robusto, antenna/rssi extracción + logger 4096 chars). Los 2 únicos pendientes son confirmaciones de hardware que dependen de probar una etiqueta nueva:
  - [ ] antenna/rssi aparecen en admin (código listo, falta correr un FX post).
  - [ ] MATCH=SI real con chip con EPC bien grabado (LAB-000012 no sirve porque salió sin ^RS/^RB, hay que reimprimir).
- **FASE 4 = 0% (como corresponde)**: se mantiene off hasta confirmar lectura 100% real + match SI en producción.
  - [x] 2026-10-05: API REST del encuadre de recepción lista (`POST/GET .../compras/recepcion-rfid-encuadres/`, `.../lecturas/`, `.../aceptar/`, `GET .../onboarding/` con candidatos OC/OP+almacenes), soporta OC y OP, `esperado` ya resta lo recibido. Documentado en `DOCUMENTACION_API.md`. Sigue sin usarse en producción — falta que Next.js lo consuma y falta el match SI real con hardware.

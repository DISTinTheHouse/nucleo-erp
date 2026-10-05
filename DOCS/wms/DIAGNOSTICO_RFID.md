# Diagnóstico RFID / Scanner / Impresora — Backend

> Auditoría del código real del backend (Django) contra lo que afirma `PLAN_RFID.md`.
> Última actualización: 2026-10-05.
> Este archivo se actualiza conforme avance la integración — no crear uno nuevo por cada revisión.

## Resumen rápido

| Componente | Estatus | Nota |
|---|---|---|
| Impresión Zebra normal (ZPL base) | ✅ Funcional | Solo disponible vía vistas QA, no vía API REST |
| Grabación RFID (EPC + ZPL RFID) | ✅ Funcional y testeado | Secuencia ZPL validada empíricamente solo en ZD621R |
| Lectura/Scanner RFID (webhook hardware) | ⚠️ Funcional con hueco de seguridad | Webhook sin autenticación |
| API REST para crear/imprimir etiquetas | ❌ No existe | El viewset solo permite list/retrieve |
| API REST de encuadre de recepción (OC/OP) | ✅ Lista para consumir | Backend completo, falta confirmar consumo real desde Next.js |
| Automatización WMS con RFID (picking/packing/despacho) | ❌ 0% | Fase 4, no iniciada (correcto según plan) |

---

## 1. Impresión Zebra normal (ZPL base)

**Funciona.** `RFIDLabelService._build_zpl_normal` genera ZPL genérico reutilizable.

**Pendiente:** no existe ningún endpoint REST tipo `GET /api/v1/wms/etiquetas/.../zpl/`. Las rutas que el plan lista como "ejemplos" son aspiracionales, nunca se implementaron. Hoy la única forma de imprimir es la vista clásica de Django en QA (`qa_guardar_impresion_sku` / `qa_guardar_impresion_oc`), no hay nada que el frontend Next.js pueda consumir directamente para esto.

## 2. Grabación RFID (EPC + ZPL RFID)

**Funciona y está testeado.**

- Modelos `EtiquetaRFIDImpresion` / `EtiquetaRFIDDetalle` en `wms/models.py` — `epc` es único e indexado, `estado` tiene los 4 valores esperados (PENDIENTE/IMPRESO/LEIDO/CANCELADO).
- El EPC generado tiene 24 hex (96 bits): `prefix(8, hash determinístico) + timestamp_chunk(4) + índice(4) + random(8)`.
- Maneja colisiones de EPC: si el EPC lo manda el cliente y choca, devuelve error 409; si lo genera el backend, reintenta hasta 3 veces.
- Tests sólidos en `wms/tests.py` (479 líneas): EPC, ZPL, colisiones, flujo feliz.

**Riesgo a vigilar:** la secuencia ZPL (`^RB96,,,1` / `^RS8,E` / `^RFW,E`, repetida dos veces + validación) se ajustó empíricamente contra una impresora **ZD621R**. El propio código documenta que esta secuencia podría no funcionar igual en otro modelo de impresora Zebra. Si en algún momento cambian de hardware, hay que re-validar.

**Hueco:** solo cubre modelo/servicio, no hay tests HTTP end-to-end de las vistas que realmente disparan la impresión.

## 3. Lectura / Scanner RFID

**Funciona, pero con un problema de seguridad real.**

- El webhook que recibe los eventos del lector de hardware (`scanner_rfid_receive`) **no tiene ninguna autenticación** — solo tiene `@csrf_exempt`. Cualquiera que conozca la URL puede mandarle datos falsos (inyectar scans falsos en el sistema).
- Esto es más grave de lo que sugiere el plan ("95% ✅, solo faltan confirmaciones de hardware"): es una brecha de seguridad, no un detalle de hardware.
- El resto de endpoints de scanner (`scanner_rfid_get`, `scanner_rfid_clear`, `scanner_rfid_stats`) sí están protegidos con `@login_required`.
- `RfidScan.antenna` / `rssi` son nullable — confirma el pendiente que el plan ya reconocía: depende de qué llaves exactas manda el lector FX.

**Para producción, antes de avanzar la Fase 3 a "completa": agregar autenticación al webhook (API key o allowlist de IP del lector).**

## 4. API REST de negocio (`/api/v1/`)

- `wms/api/urls.py` registra `etiquetas-rfid` → `EtiquetaRFIDViewSet`, **pero este viewset solo permite `list` y `retrieve`** — no tiene `create` ni ninguna acción para generar ZPL o grabar una etiqueta. No sirve para que el frontend imprima/grabe, solo para consultar lo ya impreso.
- `compras/api/urls.py` registra `recepcion-rfid-encuadres` (encuadre de recepción contra OC/OP + almacenes) — **este sí está completo del lado backend**: `onboarding/` con candidatos, `lecturas/`, `aceptar/`. Documentado en `DOCUMENTACION_API.md`.
- **Conclusión para el equipo de Next.js:** lo único que hoy está "listo para consumir" vía API REST es el encuadre de recepción RFID. Imprimir/grabar etiquetas NO tiene API REST — solo existe como vista Django clásica dentro de QA, pensada para pruebas internas, no para integrarse desde el frontend de producción.

## 5. Automatización WMS con RFID (Fase 4)

0%, como corresponde según el plan. No hay integración de RFID en picking, packing, despacho ni recepción automática todavía — solo el encuadre de recepción vía API, sin confirmación de que el frontend ya lo esté consumiendo.

---

## Pendientes para cerrar el ciclo

1. **Seguridad del webhook `scanner_rfid_receive`**: agregar autenticación (API key o allowlist de IP del lector FX) antes de exponerlo fuera de un entorno controlado.
2. **Decidir si se expone vía API REST** la creación de etiquetas RFID (el viewset actual no lo permite) — necesario si Next.js va a imprimir/grabar sin pasar por las vistas internas de QA.
3. **Confirmar con hardware real**: que `antenna`/`rssi` dejen de salir nulos, y lograr un MATCH=SI real con una etiqueta reimpresa con el ZPL corregido.
4. **Validar la secuencia ZPL en otro modelo de impresora** si se va a soportar hardware distinto al ZD621R.
5. **Tests end-to-end** de las vistas QA y del viewset REST (hoy solo hay tests de servicio/modelo).
6. Fase 4: no iniciar automatización de WMS hasta tener lectura 100% confiable y el match real validado en campo.

---

## Referencia

Plan original y roadmap de fases: [`PLAN_RFID.md`](PLAN_RFID.md)

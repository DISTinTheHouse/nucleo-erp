# Fixes de seguridad: aislamiento multi-tenant en RFID/QA

**Rama:** `fix/seguridad-multi-tenant-rfid-qa` · **Fecha:** 2026-09-30
**Origen:** auditoría de un compañero sobre el flujo Calidad/Recepción, verificados en código antes de corregir (no se tomaron a ciegas).

## Hallazgos y fix

| # | Dónde | Problema | Fix |
|---|---|---|---|
| S1 | `QA/views.py` | `scanner_rfid_clear`/`_get`/`_stats` sin ningún chequeo de auth — cualquiera podía borrar toda la tabla `RfidScan` con un simple GET | `@login_required` en los 3; `clear` además exige `is_superuser`/`is_admin_empresa` |
| S2 | `QA/views.py` | `_empresa_qa` caía a `Empresa.objects.first()` si el usuario no tenía empresa asignada | Devuelve `None` (los 5 call-sites ya manejaban ese caso correctamente) |
| S2-bis | `QA/views.py` | `generar_orden_produccion` tenía el mismo patrón, pero escribiendo una `OrdenProduccion` real contra la primera empresa de la BD | Usa `request.user.empresa`; `Sucursal` validada contra esa empresa |
| S3 | `wms/api/views.py` | `scanner-stats` exponía los mismos datos crudos que `scans_clear` sin exigir el mismo permiso | Exige `is_superuser`/`is_admin_empresa` |
| S4 | `compras/api/views.py` | El picker de ubicaciones por `almacen_id` en `recepciones/onboarding` no validaba que el almacén fuera de la empresa del usuario | Filtra por `almacen__empresa` |
| S5 | `wms/api/views.py` | `/etiquetas-rfid/scans/` devolvía EPC/antena/RSSI/IP crudos de lecturas de **otras empresas** marcados como "no match" (`RfidScan` no tiene FK a empresa) | Un no-superusuario solo ve lecturas que hacen match con una etiqueta de su propia empresa; superuser conserva la vista completa para debug |

## Verificación

- Script dedicado: 2 empresas + 3 scans (uno por empresa + uno sin match) — confirma que cada empresa ve solo el suyo, superuser ve los 3.
- `scanner-stats`: 403 para usuario normal, 200 para superuser.
- `manage.py test compras produccion wms QA` → 260 tests, OK.

## Pendiente (fuera de alcance de este fix)

- El `<select>` de variantes en `generar_orden_produccion` (GET, solo lectura) sigue sin filtrar por empresa. No se tocó para no ampliar el alcance de este cambio.

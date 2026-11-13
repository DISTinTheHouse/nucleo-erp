# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

The **Núcleo (Core)** of a multi-tenant ERP, built with **Django 6.0 + Django REST Framework**. It runs headless/API-first: Django is the source of truth and exposes a secure REST API consumed by a separate Next.js frontend. Django's own HTML pages exist only for internal/technical admin (Core dashboard, superuser CRUD). Domain and code are in Spanish (`LANGUAGE_CODE = es-mx`); the business domain is Mexican fiscal/SAT compliance for an apparel manufacturer (embroidery, screen-printing, cut-and-sew).

## Commands

```bash
# Setup
python -m venv venv && .\venv\Scripts\activate   # Windows
pip install -r requirements.txt          # requirements-dev.txt adds test-only deps (tblib for --parallel)

# Run (note: README uses port 8003; docker-compose uses 8000)
python manage.py runserver 0.0.0.0:8003

# Migrations
python manage.py makemigrations
python manage.py migrate
python manage.py makemigrations --dry-run --check   # what CI enforces — must report "No changes"

# Validation (this is the full CI gate — tests are not run in CI, see Pruebas below)
python manage.py check

# Tests — ONLY with the local-Postgres settings (see Pruebas below)
python manage.py test --settings=ERP.settings_test --keepdb --parallel 4 ventas compras   # whole suite ~1.5 min
python manage.py test --settings=ERP.settings_test --keepdb ventas.tests.PedidoKpisTests  # one class: seconds

# Operational
python manage.py axes_reset                # clear brute-force lockouts (run after lockout during dev)
python manage.py populate_sat_catalogs     # seed SAT fiscal catalogs (regímenes, uso CFDI, métodos/formas de pago)
python manage.py collectstatic --noinput

# Docker (single `web` service on port 8000; runs ONLY `runserver` — never auto-migrates)
docker compose up --build
docker compose run --rm web python manage.py migrate   # migrating is manual and deliberate — see note below
```

**Docker/compose is a local convenience and is intentionally NOT tracked in git.** `docker-compose.yml` and its `Dockerfile` exist only to run the API on a host without Python installed — they are not part of the repo and must not be committed or added. The compose file defines **only** the `web` container: there is no bundled Postgres, and the `.env` it reads points at the **production** Supabase DB.

Because of that, the `web` service runs **only** `runserver` and must **never** auto-run `migrate` on `up` — doing so applied every unapplied migration straight to production on each start. Migrating is a deliberate, manual action: `docker compose run --rm web python manage.py migrate`. Production migration policy lives in the `migrate_production` job (see below), not in compose.

### Pruebas — Postgres local, nunca Supabase ni SQLite

- **Tests run only with `--settings=ERP.settings_test`**, against a local Postgres 17 in Docker (`nucleo-erp-db`, `127.0.0.1:5432`). `ERP/settings_test.py` builds the DB from `LOCAL_POSTGRES_*` regardless of `USE_REMOTE_DB` and **refuses to start if the host is not local**. Django creates `test_erp_pruebas` (and `_1`…`_N` clones with `--parallel`) on that server; `--keepdb` reuses it, so migrations only run the first time (~5 min) or when new ones appear. Use `--parallel 4`: on Windows more workers are slower (process spawn + Docker). `tblib` (`requirements-dev.txt`) is needed for `--parallel` to report failures.
- **Never run a bare `manage.py test`** (without `--settings=ERP.settings_test`): with the default settings it creates its test database on the production Supabase (it happened on 2026-08-03 and left an orphaned `test_postgres`). SQLite was dropped on purpose (`a316b99`) and must not come back.
- **Local setup**: `docker run -d --name nucleo-erp-db --restart unless-stopped -p 127.0.0.1:5432:5432 -v nucleo-erp-db-data:/var/lib/postgresql/data -e POSTGRES_USER=postgres -e POSTGRES_PASSWORD=<local> -e POSTGRES_DB=erp_restore postgres:17`, then `CREATE EXTENSION pg_trgm` in `template1` (the `gin_trgm_ops` indexes need it and no migration creates it). `.env` points `LOCAL_POSTGRES_*` at it (host `127.0.0.1`, db `erp_restore`, user `postgres`).
- **`erp_restore` is a copy of Supabase** (`pg_dump --schema=public --no-owner --no-privileges` through the **session** pooler, port 5432, then `pg_restore`; create `pg_trgm` first, `--schema` dumps omit extensions). Run the API or verification scripts against it with `USE_REMOTE_DB=False`: same data, ~10–100 ms per request instead of hundreds. Refresh it when you need newer data; it holds personal data, so dumps never go into the repo.
- **Verify behavior against real data** (permissions catalog, users, SAT catalogs only exist in the DB) from a `manage.py shell -c` script with DRF's `APIClient` + `force_authenticate`, preferably with `USE_REMOTE_DB=False` on the local copy; use Supabase only when the live state matters:
  - reads: plain `GET`s;
  - writes and migrations: inside `transaction.atomic()` and raise an exception at the end so everything rolls back (`MigrationExecutor(connection).migrate(...)` for an unapplied migration).
- While iterating, run only the test classes the change touches; the whole suite once at the end. `*/tests.py` are not a CI gate yet (CI is `manage.py check` + migration-drift check only), but keep them updated and green locally.

### Prioridad 1: velocidad de los endpoints

La app está por liberarse a la operación real: **ningún endpoint puede sentirse lento** (2 s es inaceptable). Antes de agregar un campo o una acción:

- Mide con `CaptureQueriesContext` antes y después (y `EXPLAIN ANALYZE` si cambia el SQL). El número de consultas no debe crecer con las filas: nada de N+1, `select_related`/`prefetch_related` o agregados en bloque.
- Prefiere anotar en la misma consulta (subconsultas por FK indexada) o una sola consulta agrupada por página antes que trabajo por fila en el serializer.
- Los listados ya optimizados (OC, recepciones, etc.) no se tocan si no es necesario; un campo nuevo no puede agregar viajes a la BD.
- Reporta en el PR las consultas y el tiempo antes/después.

## Database & environments

DB selection is driven by env vars, not Django settings files (`ERP/settings.py` is the runtime settings module; `ERP/settings_test.py` exists only for `manage.py test`, see Pruebas):

- **`USE_REMOTE_DB`** (defaults true on Vercel or `ENVIRONMENT=production`) toggles local Postgres vs. remote.
- Remote uses `DATABASE_URL` / `SUPABASE_DATABASE_URL` (Supabase), with SSL required and server-side cursors disabled (required for Supabase's transaction pooler).
- Local uses `LOCAL_POSTGRES_*` vars (default db `erp` on `127.0.0.1:5432`).
- `SECRET_KEY` is the only hard-required var; nearly everything else has a default via `python-decouple`'s `config()`.

**Migrations are NOT applied on deploy.** Vercel runs serverless and never migrates. Production migrations run from the GitHub Actions `vercel.yml` workflow (`migrate_production` job) against Supabase on push to `main`. The `workflow_dispatch` "maintenance" path exists to apply `--fake` migrations when state drifts. Keep this in mind before assuming a model change is live.

## Architecture

### Apps and their domains
- **`nucleo`** — multi-tenant core: `Empresa` (tenant), `Sucursal`, `Departamento`, `SerieFolio` (document numbering), shared catalogs (`Moneda`, `Impuesto`, `UnidadMedida`), and SAT fiscal catalogs. Also holds project-wide middleware and mixins.
- **`usuarios`** — custom user model (see below), 2FA, email-based auth backend.
- **`seguridad`** — RBAC: `Permiso`, `Rol`, `RolPermiso`, `UsuarioRol`, plus `UsuarioPermiso` grant/deny overrides.
- **`auditoria`** — audit trail (`AuditoriaEvento`) of critical writes. Also `BitacoraDocumento` (`bitacora_documentos`): a light document history (create / status change / deactivate / restore / delete, no JSON) filled by signals in `auditoria/bitacora.py` (`DOCUMENTOS` maps each model); read-only API at `/api/v1/auditoria/documentos/` (+ `linea-tiempo/`). `QuerySet.update()` bypasses it. To track a new document, add it to `DOCUMENTOS`.
- **`terceros`** — clients/suppliers; RFC validation (SAT checksum) and Facturama (CFDI invoicing) integration.
- **`catalogo`**, **`compras`**, **`ventas`**, **`produccion`**, **`logistica`** — business modules. Sales = `Cotizacion`/`Pedido`; production = embroidery/reflejante/corte-manga orders + BOM (`ListaMaterialBom`/`BomDetalle`); an OP can cover several pedidos (`OrdenProduccionPedido`, `pedidos_ids`/`pedidos`; `OrdenProduccion.pedido` stays the principal one) and outsourced confection is tracked with `SalidaMaquila` (folio SM, document only, no inventory movement — the OP already consumed the material); purchasing = `Requisicion` → `SolicitudCompra` → `OrdenCompra` → `Recepcion`, where `Recepcion` has a dual-origin pattern (`tipo_origen` + separate FKs) receiving against either an `OrdenCompra` (OC) or a `produccion.OrdenProduccion` (OP).
- **`inventarios`** — the stock ledger: `Almacen`/`Ubicacion`, `Existencia` (on-hand quantity per producto/variante + almacén/ubicación), `MovimientoInventario`/`MovimientoInventarioDetalle`, `AjusteInventario`. `OperacionInventarioViewSet` exposes a unified `entrada`/`salida`/`ajuste` API (optionally linked to a `Pedido`), plus paginated `reporte-existencias-periodo`/`reporte-movimientos-periodo` endpoints. Writes are posted here by `compras.Recepcion`, `produccion.OrdenProduccion`, and `wms.Transferencia` — `wms` orchestrates the warehouse workflows, `inventarios` is the ledger they write to. `Lote`/`Serie` models exist but carry no fields beyond a `producto` FK and no seeding path — lot/serial tracking is not functional today.
- **`wms`** — warehouse execution, split from the generic business modules above given its distinct model set: `Picking`/`PickingDetalle` (order/batch/wave/zone picking against a `Pedido`), `Packing`, `Despacho`, `ConteoCiclico` (cycle counts), and `Transferencia` (inter-warehouse stock transfers).
- **`finanzas`** — accounting & treasury: chart of accounts (`CuentaContable`), cost centers, journal entries (`Poliza`/`PolizaDetalle`), customer/supplier invoices (`Factura`/`FacturaProveedor`), AR/AP (`CuentaPorCobrar`/`CuentaPorPagar`), collections/payments (`Cobro`/`Pago`), banks & bank movements, reconciliation, credit notes. API at `/api/v1/finanzas/`.
- **`ia`** — AI assistant (OpenAI) and Google Drive (OAuth 2.0) integration. Note the API mounts at **`/api/v1/ai/`**, not `/api/v1/ia/`.

### Two distinct auth surfaces — keep them straight
1. **Web Core (HTML, internal)**: session auth + `django-axes` brute-force lockout + optional email/SMS 2FA. Login at `/`, 2FA at `/two-factor/`. Protected with `LoginRequiredMixin` + `UserPassesTestMixin`/`SuperuserRequiredMixin`. **Gotcha:** login is by email (`USERNAME_FIELD = 'email'`) but the POST field and axes config use the name `username` (`AXES_USERNAME_FORM_FIELD = 'username'`), and a custom `usuarios.backends.EmailBackend` resolves it.
2. **REST API (`/api/v1/...`)**: JWT-in-cookie via `auth_kit` (`JWTCookieAuthentication`), `IsAuthenticated` by default. Auth endpoints under `/api/auth/`; MFA enabled via `AUTH_KIT['USE_MFA']`. OpenAPI docs at `/api/docs/` (Swagger) and `/api/redoc/`, schema at `/api/schema/`.

### Multi-tenant isolation — the single most important pattern
The DB is shared; isolation is enforced in code, never trusted from the client. **Every API ViewSet over tenant data overrides `get_queryset()`** to filter by the requesting user's company. The canonical shape (see `ventas/api/views.py`):

```python
def get_queryset(self):
    user = self.request.user
    qs = super().get_queryset().select_related(...)
    if user.is_superuser:
        return qs                      # global staff
    empresa = getattr(user, "empresa", None)
    if empresa:
        qs = qs.filter(empresa=empresa)
        if not user.is_admin_empresa:  # non-admins are further scoped (e.g. own records)
            qs = qs.filter(vendedor=user)   
        return qs
    return qs.none()                   # no company -> see nothing
```

Expected behavior: list returns `200 []` when out of scope; detail returns `404` (not 403) for another company's existing record. **When adding any tenant-scoped endpoint, replicate this — a missing `get_queryset` override is a cross-tenant data leak.**

**One company at a time (EC-816).** Data is always scoped to the *active* `user.empresa`; the `empresas` M2M never widens reads or writes. Use `nucleo.scope.empresa_ids_lectura(user)` instead of re-deriving it. **Only superusers switch the active company (EC-843)**, via `POST /api/v1/nucleo/cambiar-empresa/` `{"empresa": <id>}` to any active company: it resets their `sucursal_default` (first active branch by `codigo`, or `null`), clears `sucursales`/`departamentos`, writes an `AuditoriaEvento` and returns the `/api/auth/user/` payload. The `empresas` M2M does not grant switching yet. `/api/v1/usuarios/` is scoped to the active company for superusers too and only moves other users to an active company; nobody changes their own `empresa` there, in the Web Core user form or in Django admin (read-only for yourself).

**`ventas`, `compras`, `finanzas` and `terceros` scope superusers too (EC-798).** `ventas/scope.py` (pedidos, cotizaciones, oportunidades, Mesa de Control, the global search), plus `compras` (calidad, encuadres RFID), `finanzas` (`_puede_ver_todo` is always `False`, so `?empresa_id=` or a body `empresa` no longer open another company) and `terceros/scope.py` (clientes, direcciones, verificar-rfc, kpis), and their write validators use the superuser's active `user.empresa` like everyone else: whole company, no seller sub-scope, never other companies, empty without an empresa. This is the rule for the whole ERP; within the empresa the superuser is never sub-scoped (no seller or sucursal filter).

**`produccion` scopes superusers too.** Per decision #361, `orden-produccion` (list/detail and `kpis`), `lista-material`, `bom-detalle`, `consumo`, `producto-terminado-entradas` and `pedidos-especiales` always use `user.empresa`, superuser included: no `if user.is_superuser: return qs` branch, empty lists and `400` on writes without an empresa, and writable FKs validated against `user.empresa` (`_fk_de_la_empresa_del_usuario` in `produccion/api/serializers.py`). Since EC-880 bordado/reflejante/corte de manga follow the same rule (`produccion/scope.py` and `_OrdenPadreTenantScopedMixin` for their avances/incidencias): the superuser sees only the active empresa, all its sucursales; another empresa's order is a `404` for read, edit, delete and avances.

**`wms` scopes superusers too (EC-882).** Picking, packing and transferencias (`wms/scope.py`, `documentos_wms_visibles`) follow the same rule: active `user.empresa`, superuser included, all its sucursales for superuser and admin, `sucursales_permitidas()` for the rest; another empresa's document is a `404` (also for `trasladar`). Despacho and etiquetas RFID still have their own `get_queryset` with the old superuser branch.

**`finanzas` scopes the commercial side by sucursal (#753).** Facturas, notas de crédito, CxC, cobros and CxC mora alerts also filter by `sucursales_permitidas()` for non-admins, via `finanzas/scope.py`. Pólizas, banks, CxP, conciliaciones and reports stay empresa-wide on purpose.

### RBAC + overrides
`Usuario.tiene_permiso(clave)` resolves effective permissions with strict precedence: superuser → `is_admin_empresa` → explicit **DENY** override → role grants → explicit **GRANT** override. Permission keys live in `seguridad.Permiso.clave` (the catalog lives **only in the DB** — there is no fixture or seed).

Two ways to ask, one rule: `tiene_permiso(clave)` for a single key, and **`nucleo.permisos.permisos_efectivos(user)`** for the whole set at once (2 queries, 0 for superuser/admin, membership in O(1) afterwards). The batch resolver is what the login API and `UsuarioSerializer.get_permisos` use for the `(roles + grants) - denies` list they ship to the frontend, and what the search endpoint uses to filter. Do **not** write a third copy of that calculation — the precedence used to be duplicated across three call sites.

**Most endpoints still don't enforce permissions server-side** — the frontend reads the login list and decides. Exceptions: finanzas (`PermisoFinanzas`); the CRM reads (#1020) — cotizaciones, oportunidades and clientes list/retrieve/`kpis`/`embudo` — via `nucleo.permisos.LecturaPorCodigo` + a `codigos_lectura` dict per ViewSet (codes in `nucleo.permisos.CODIGOS_*`, same as the frontend `routePermissions.ts`; detail also accepts the `/orders/[id]` codes); and `/api/v1/search/`, which filters *which entity groups* it returns by the user's real permissions (each `EntidadBuscable` declares its own `permisos_visibilidad`). Entity visibility is separate from row scoping, which stays in the `scope.py` predicates.

### Conventions for new code
- **API apps follow `{{app}}/api/` layout**: `urls.py` (DRF `DefaultRouter`), `views.py` (ViewSets), `serializers.py`. Wire the router into `ERP/urls.py` under `/api/v1/{{app}}/`. ViewSets commonly override `get_serializer_class()` per action (list/retrieve/default) and restrict `http_method_names`. **Documented exception:** `/api/v1/search/` (global header search) has no app segment because it federates across apps; it's an `APIView` in `nucleo/api/search.py`, routed from `nucleo/urls.py` alongside nucleo's other `APIView`s. Add cross-app endpoints there, not as a new top-level entry in `ERP/urls.py`.
- **`Usuario`** (`usuarios.Usuario`, `AUTH_USER_MODEL`) extends `AbstractUser`, logs in by email, and carries multi-tenant scope: `empresa` (active) + `empresas`/`sucursales`/`departamentos` (M2M access scopes). `estatus` is kept in sync with `is_active` in `save()`.
- **Soft delete**: models inheriting `nucleo.models.StatusLifecycleModel` use an `activo` flag with `.soft_delete()` / `.restore()` — prefer this over hard deletes for catalog/tenant data.
- **Auditing**: web CRUD views mix in `nucleo.mixins.AuditLogMixin` (writes `AuditoriaEvento`, diffs changed fields). API requests are logged by `nucleo.middleware.APILoggingMiddleware` to `logs/api.log`; `NoCacheMiddleware` disables caching on `/api/`.
- **Tables and verbose names are explicit and Spanish** (`db_table = "usuarios"`, etc.). Match the surrounding Spanish naming and existing `Meta` style when adding models.
- MFA migrations are redirected via `MIGRATION_MODULES = {{"mfa": "ERP.mfa_migrations"}}`.

## Deployment

- **Primary: Vercel** serverless. `vercel.json` declares an explicit `builds` array pointing at `ERP/wsgi.py` (which exports `app`), so that's the active entrypoint — Vercel ignores `/api` zero-config auto-detection once `builds` is set. `api/index.py` (also `ERP.wsgi.application`, just via the zero-config convention) has no commits since the explicit `builds` config was restored; treat it as likely-dead, not the documented entry, unless verified otherwise against the Vercel dashboard. Logs go to `/tmp/logs` (non-persistent). `/healthz/` is the health endpoint — liveness only (`{"ok": true}`), does not check DB connectivity.
- **Contingency: Render** via `render.yaml` + `build.sh` (gunicorn; `build.sh` does run `migrate` + `collectstatic` at build time, unlike Vercel where migration is a separate GitHub Actions job — behavior differs under the same code if Render is ever promoted to primary).
- CI/CD in `.github/workflows/vercel.yml`: PR → `ci` (`manage.py check` + migration-drift check; no tests); push to `main` → `ci`, then `migrate_production` (Supabase migrate + axes_reset), then `deploy_production` (`vercel deploy --prod`). The workflow rejects a Supabase URL using the Session pooler (port 5432) — production must use the Transaction pooler (6543). Full pipeline detail: `DOCS/arquitectura/pipeline-despliegue-backend.md`.

## Reference docs (Spanish, under `DOCS/`)
`DOCS/api/DOCUMENTACION_API.md` (endpoint reference), `DOCS/arquitectura/ARQUITECTURA_APP.md` (architecture & security hardening), `DOCS/arquitectura/ESQUEMA_BD.md` (data model), `DOCS/seguridad/CIBERSEGURIDAD.md`/`DOCS/seguridad/SECURITY.md` (security), `DOCS/negocio/ASISTENTE_IA.md` (AI assistant), `DOCS/negocio/GUIA_USUARIO.md` (user manual). Deeper investigation write-ups (architecture audits, flow traces) live alongside them under `DOCS/arquitectura/` and `DOCS/api/`; `DOCS/wms/` and `DOCS/varios/` hold internal planning notes; `DOCS/datos/` holds data exports, not docs.

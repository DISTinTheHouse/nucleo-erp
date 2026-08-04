# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

The **Núcleo (Core)** of a multi-tenant ERP, built with **Django 6.0 + Django REST Framework**. It runs headless/API-first: Django is the source of truth and exposes a secure REST API consumed by a separate Next.js frontend. Django's own HTML pages exist only for internal/technical admin (Core dashboard, superuser CRUD). Domain and code are in Spanish (`LANGUAGE_CODE = es-mx`); the business domain is Mexican fiscal/SAT compliance for an apparel manufacturer (embroidery, screen-printing, cut-and-sew).

## Commands

```bash
# Setup
python -m venv venv && .\venv\Scripts\activate   # Windows
pip install -r requirements.txt

# Run (note: README uses port 8003; docker-compose uses 8000)
python manage.py runserver 0.0.0.0:8003

# Migrations
python manage.py makemigrations
python manage.py migrate
python manage.py makemigrations --dry-run --check   # what CI enforces — must report "No changes"

# Validation (this is the full CI gate — real tests exist locally but aren't run in CI, see note below)
python manage.py check

# Operational
python manage.py axes_reset                # clear brute-force lockouts (run after lockout during dev)
python manage.py populate_sat_catalogs     # seed SAT fiscal catalogs (regímenes, uso CFDI, métodos/formas de pago)
python manage.py collectstatic --noinput

# Docker (single `web` service on port 8000; runs `migrate` then `runserver`)
docker compose up --build
```

Note: `docker-compose.yml` defines **only** the `web` container — there is no bundled Postgres. It builds from the `Dockerfile`, reads config from a `.env` file, and expects a reachable DB (host Postgres or remote Supabase) per the env vars below.

Test coverage is uneven — `produccion/tests.py` (58 tests), `ventas/tests.py` (7 tests), and `wms/tests.py` (25 tests) contain real tests; no other app has a `tests.py` file. (Grepping for `def test_func` elsewhere in the codebase will hit `UserPassesTestMixin` permission checks, not tests.) None of this runs in CI — the CI gate is still `manage.py check` + migration-drift check.

## Database & environments

DB selection is driven by env vars, not Django settings files (`ERP/settings.py` is the only settings module):

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
- **`auditoria`** — audit trail (`AuditoriaEvento`) of critical writes.
- **`terceros`** — clients/suppliers; RFC validation (SAT checksum) and Facturama (CFDI invoicing) integration.
- **`catalogo`**, **`compras`**, **`ventas`**, **`produccion`**, **`logistica`** — business modules. Sales = `Cotizacion`/`Pedido`; production = embroidery/reflejante/corte-manga orders + BOM (`ListaMaterialBom`/`BomDetalle`); purchasing = `Requisicion` → `SolicitudCompra` → `OrdenCompra` → `Recepcion`, where `Recepcion` has a dual-origin pattern (`tipo_origen` + separate FKs) receiving against either an `OrdenCompra` (OC) or a `produccion.OrdenProduccion` (OP).
- **`inventarios`** — the stock ledger: `Almacen`/`Ubicacion`, `Existencia` (on-hand quantity per producto/variante + almacén/ubicación), `MovimientoInventario`/`MovimientoInventarioDetalle`, `AjusteInventario`. `OperacionInventarioViewSet` exposes a unified `entrada`/`salida`/`ajuste` API (optionally linked to a `Pedido`), plus paginated `reporte-existencias-periodo`/`reporte-movimientos-periodo` endpoints. Writes are posted here by `compras.Recepcion`, `produccion.OrdenProduccion`, and `wms.Transferencia` — `wms` orchestrates the warehouse workflows, `inventarios` is the ledger they write to. `Lote`/`Serie` models exist but carry no fields beyond a `producto` FK and no seeding path — lot/serial tracking is not functional today.
- **`wms`** — warehouse execution, split from the generic business modules above given its distinct model set: `Picking`/`PickingDetalle` (order/batch/wave/zone picking against a `Pedido`), `Packing`, `Despacho`, `ConteoCiclico` (cycle counts), and `Transferencia` (inter-warehouse stock transfers).
- **`finanzas`** — accounting & treasury: chart of accounts (`CuentaContable`), cost centers, journal entries (`Poliza`/`PolizaDetalle`), customer/supplier invoices (`Factura`/`FacturaProveedor`), AR/AP (`CuentaPorCobrar`/`CuentaPorPagar`), collections/payments (`Cobro`/`Pago`), banks & bank movements, reconciliation, credit notes. API at `/api/v1/finanzas/`.
- **`QA`** — quality module; currently a stub (empty models, HTML urls under `/QA/`).
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

### RBAC + overrides
`Usuario.tiene_permiso(clave)` resolves effective permissions with strict precedence: superuser → `is_admin_empresa` → explicit **DENY** override → role grants → explicit **GRANT** override. Permission keys live in `seguridad.Permiso.clave`. The login API computes the effective `(roles + grants) - denies` list and ships it to the frontend, which only reads the final list.

### Conventions for new code
- **API apps follow `{{app}}/api/` layout**: `urls.py` (DRF `DefaultRouter`), `views.py` (ViewSets), `serializers.py`. Wire the router into `ERP/urls.py` under `/api/v1/{{app}}/`. ViewSets commonly override `get_serializer_class()` per action (list/retrieve/default) and restrict `http_method_names`.
- **`Usuario`** (`usuarios.Usuario`, `AUTH_USER_MODEL`) extends `AbstractUser`, logs in by email, and carries multi-tenant scope: `empresa` (active) + `empresas`/`sucursales`/`departamentos` (M2M access scopes). `estatus` is kept in sync with `is_active` in `save()`.
- **Soft delete**: models inheriting `nucleo.models.StatusLifecycleModel` use an `activo` flag with `.soft_delete()` / `.restore()` — prefer this over hard deletes for catalog/tenant data.
- **Auditing**: web CRUD views mix in `nucleo.mixins.AuditLogMixin` (writes `AuditoriaEvento`, diffs changed fields). API requests are logged by `nucleo.middleware.APILoggingMiddleware` to `logs/api.log`; `NoCacheMiddleware` disables caching on `/api/`.
- **Tables and verbose names are explicit and Spanish** (`db_table = "usuarios"`, etc.). Match the surrounding Spanish naming and existing `Meta` style when adding models.
- MFA migrations are redirected via `MIGRATION_MODULES = {{"mfa": "ERP.mfa_migrations"}}`.

## Deployment

- **Primary: Vercel** serverless. Entry is `api/index.py` → `ERP.wsgi.application`; routing in `vercel.json`. Logs go to `/tmp/logs` (non-persistent). `/healthz/` is the health endpoint.
- **Contingency: Render** via `render.yaml` + `build.sh` (gunicorn; `build.sh` does run `migrate` + `collectstatic`, unlike Vercel).
- CI/CD in `.github/workflows/vercel.yml`: PR → checks; push to `main` → checks, Supabase migrate, axes_reset, then `vercel deploy --prod`. The workflow rejects a Supabase URL using the Session pooler (port 5432) — production must use the Transaction pooler (6543).

## Reference docs (Spanish, in repo root)
`ARQUITECTURA_APP.md` (architecture & security hardening), `DOCUMENTACION_API.md` (endpoint reference), `ESQUEMA_BD.md` (data model), `CIBERSEGURIDAD.md`/`SECURITY.md` (security), `ASISTENTE_IA.md` (AI assistant), `GUIA_USUARIO.md` (user manual).

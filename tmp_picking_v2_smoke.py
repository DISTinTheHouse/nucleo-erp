import os, sys, random, string
import django, decimal

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "ERP.settings")
django.setup()

D = decimal.Decimal
from django.test.utils import override_settings
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from django.db import transaction

from inventarios.models import Almacen, Existencia
from catalogo.models import Producto, ProductoVariante, Talla, Color, TipoProducto, CategoriaProducto
from ventas.models import Cliente, Prospecto, Oportunidad, Cotizacion, CotizacionDetalle, CotizacionDetalleTalla, Pedido, PedidoDetalle, PedidoDetalleTalla
from nucleo.models import Sucursal, Empresa, SerieFolio, Moneda
from django.db.models import Q

User = get_user_model()


def aeq(cond, msg):
    if not cond:
        raise AssertionError(f"FAIL: {msg}")
    print(f"  OK  {msg}")


sid = transaction.savepoint()
try:
    with override_settings(
        ALLOWED_HOSTS=["*", "testserver"],
        SECURE_SSL_REDIRECT=False,
        SECURE_HSTS_SECONDS=0,
        SECURE_HSTS_INCLUDE_SUBDOMAINS=False,
        SECURE_CONTENT_TYPE_NOSNIFF=False,
        SECURE_BROWSER_XSS_FILTER=False,
        CSRF_COOKIE_SECURE=False,
        SESSION_COOKIE_SECURE=False,
        DEBUG=True,
    ):
        print("=" * 70)
        print("E2E SMOKE — PICKING v2 (Tracker de prendas + selector destino libre)")
        print("=" * 70)

        # ---- (0) Setup de data ---------------------------------------------------
        emp = Empresa.objects.filter(activo=True).order_by("id").first()
        aeq(emp is not None, "Empresa activa exista")
        suc = Sucursal.objects.filter(empresa=emp, activo=True).order_by("id").first()
        aeq(suc is not None, "Sucursal activa exista")
        mon = Moneda.objects.filter(Q(clave__iexact="MXN") | Q(codigo__iexact="MXN")).first() or Moneda.objects.first()
        aeq(mon is not None, "Moneda MXN exista")

        # Usuario admin / superuser para APIClient force_auth
        admin = User.objects.filter(Q(is_superuser=True) | Q(is_admin_empresa=True)).first()
        if admin is None:
            admins = list(User.objects.filter(empresa=emp, is_active=True).order_by("id"))[:1]
            admin = admins[0] if admins else None
        aeq(admin is not None, "Usuario activo de la empresa exista")

        # Cliente + Prospecto + Oportunidad para poder crear Cotizacion
        cli, _ = Cliente.objects.get_or_create(
            empresa=emp, razon_social="QA SMOKE Cliente Picking",
            defaults={"rfc": "XAXX010101000", "email": "qa-smoke-picking@example.com", "telefono": "555"},
        )
        prospecto, _ = Prospecto.objects.get_or_create(
            empresa=emp, nombre="QA SMOKE Prospecto Picking",
            defaults={"email": "qa-smoke-pick@example.com", "telefono": "555"},
        )
        op, _ = Oportunidad.objects.get_or_create(
            prospecto=prospecto, titulo="QA Smoke Picking Opportunity",
            defaults={"estatus": "CERRADO_GANADA", "monto_total": 100},
        )

        # Tipo / Categoría / Producto + Talla + Variante + Color para construir PDT con lleva_bordado=True
        tp, _ = TipoProducto.objects.get_or_create(empresa=emp, nombre="QA SMOKE Tipo Picking", defaults={})
        cat, _ = CategoriaProducto.objects.get_or_create(empresa=emp, tipo=tp, nombre="QA SMOKE Cat Picking", defaults={})
        prod, _ = Producto.objects.get_or_create(
            empresa=emp, categoria=cat, codigo=f"QA-PICK-{os.getpid()}", nombre="QA SMOKE Gorra Legionario Picking",
            defaults={"activo": True, "precio_lista": D("10.00")},
        )
        prod.activo = True
        prod.save(update_fields=["activo", "updated_at"])
        talla, _ = Talla.objects.get_or_create(empresa=emp, clave="CH", defaults={"nombre": "Chica"})
        color, _ = Color.objects.get_or_create(empresa=emp, clave="NEG", defaults={"nombre": "Negro"})
        variante, _ = ProductoVariante.objects.get_or_create(
            empresa=emp, producto=prod, talla=talla, color=color,
            defaults={"sku": f"QA-PICK-VAR-{os.getpid()}", "precio": D("10.00"), "activo": True},
        )
        variante.activo = True
        variante.save(update_fields=["activo", "updated_at"])

        # ---- Setup ALMACENES con flags prendidos (CRITICAL PARA VALIDACIONES)
        def _get_or_make_almacen(nombre, codigo, tipo, salida=True, entrada=True, transferencia=False):
            a = (
                Almacen.objects.filter(empresa=emp, sucursal=suc, codigo__iexact=codigo).first()
                or Almacen.objects.filter(empresa=emp, sucursal=suc, nombre__iexact=nombre).first()
            )
            if a is None:
                a = Almacen.objects.create(
                    empresa=emp, sucursal=suc, codigo=codigo, nombre=nombre,
                    estatus="ACTIVO", tipo_almacen=tipo,
                    permite_ubicacion=False,
                    permite_entrada=entrada, permite_salida=salida,
                    permite_transferencia=transferencia,
                )
            else:
                dirty = False
                for field, val in [
                    ("permite_entrada", entrada),
                    ("permite_salida", salida),
                    ("permite_transferencia", transferencia),
                    ("estatus", "ACTIVO"),
                    ("tipo_almacen", tipo),
                    ("empresa_id", emp.pk),
                    ("sucursal_id", suc.pk),
                ]:
                    if getattr(a, field, None) != val:
                        setattr(a, field, val); dirty = True
                if dirty:
                    a.save(update_fields=["permite_entrada", "permite_salida", "permite_transferencia",
                                          "estatus", "tipo_almacen", "empresa", "sucursal", "updated_at"])
            return a

        alm_origen = _get_or_make_almacen(
            "QA SMOKE ALMACEN ORIGEN PICK MP", f"QA-PICK-ORI-{os.getpid()}", "MP",
            salida=True, entrada=False,
        )
        alm_destino_1 = _get_or_make_almacen(
            "QA SMOKE APARTADOS PICK", f"QA-PICK-APART-{os.getpid()}", "PT",
            salida=False, entrada=True,
        )
        alm_destino_2 = _get_or_make_almacen(
            "QA SMOKE PROCESO PICK DEST 2", f"QA-PICK-PRO-{os.getpid()}", "PROCESO",
            salida=True, entrada=True, transferencia=True,
        )
        aeq(alm_origen.permite_salida is True, "Almacen origen tiene permite_salida=True")
        aeq(alm_destino_1.permite_entrada is True, "Almacen APARTADOS tiene permite_entrada=True")
        aeq(alm_destino_2.permite_entrada is True, "Almacen PROCESO destino tiene permite_entrada=True")
        aeq(alm_origen.pk != alm_destino_1.pk != alm_destino_2.pk != alm_origen.pk,
            "Los 3 almacenes son distintos")

        # Existencia en origen de la variante para que el maximo_picking_permitido sea >0
        ex, _ = Existencia.objects.get_or_create(
            almacen=alm_origen, producto=prod, producto_variante=variante,
            defaults={"fisica": D("100.0000"), "disponible": D("100.0000"),
                      "reservada": D("0"), "transito": D("0")},
        )
        if ex.fisica < D("100"):
            ex.fisica = D("100.0000"); ex.disponible = D("100.0000")
            ex.save(update_fields=["fisica", "disponible", "updated_at"])

        # SerieFolio para Picking (SSoT preview sin consumo) y OrdenBordado (para shape check)
        def _sf(tipo):
            sf = SerieFolio.objects.filter(
                empresa=emp, activo=True, tipo_documento__iexact=tipo,
            ).order_by("id_serie_folio").first()
            if sf is None:
                sf = SerieFolio.objects.create(
                    empresa=emp,
                    tipo_documento=tipo,
                    serie=tipo[:3].upper(),
                    folio_actual=0,
                    folio_inicial=1,
                    reiniciar_anual=False,
                    incluir_anio=(tipo == "OrdenBordado"),
                    relleno_ceros=6,
                    separador="-",
                )
            return sf

        sf_pick = _sf("Picking")
        sf_ob = _sf("OrdenBordado")
        print(f"SF PICK id={sf_pick.pk} actual={sf_pick.folio_actual}")
        print(f"SF OB   id={sf_ob.pk} actual={sf_ob.folio_actual}")

        # Cleanup QA de corridas previas (isolation)
        from wms.models import Picking, PickingDetalle
        from produccion.models import OrdenesBordado, OrdenBordadoDetalle
        _qa_pedidos = list(Pedido.objects.filter(
            Q(cliente_razon_social__startswith="QA SMOKE")
            | Q(observaciones__startswith="QA PICK TRACKER")
            | Q(oc__startswith="QA-PICK-TRK")
        ).values_list("pk", flat=True))
        if _qa_pedidos:
            OrdenBordadoDetalle.objects.filter(ob__pedido_id__in=_qa_pedidos).delete()
            OrdenesBordado.objects.filter(pedido_id__in=_qa_pedidos).delete()
            _picks = list(Picking.objects.filter(pedido_id__in=_qa_pedidos).values_list("pk", flat=True))
            if _picks:
                PickingDetalle.objects.filter(picking_id__in=_picks).delete()
                Picking.objects.filter(pk__in=_picks).delete()
            PedidoDetalleTalla.objects.filter(pedido_detalle__pedido_id__in=_qa_pedidos).delete()
            PedidoDetalle.objects.filter(pedido_id__in=_qa_pedidos).delete()
            Pedido.objects.filter(pk__in=_qa_pedidos).delete()
        Cotizacion.objects.filter(
            Q(observaciones__startswith="QA PICK TRACKER") | Q(oc__startswith="QA-PICK-TRK")
        ).delete()
        sf_ob.folio_actual = 0
        sf_ob.save(update_fields=["folio_actual", "updated_at"])
        sf_pick.refresh_from_db()
        sf_ob.refresh_from_db()
        print("Cleanup QA previo OK. Serie OB reset 0.")

        # ---- Construir PEDIDO con PDTs (lleva_bordado=True)
        ped = Pedido.objects.create(
            empresa=emp, sucursal=suc, cliente=cli,
            folio=f"QA-PICK-TRK-{os.getpid()}-{''.join(random.choices(string.ascii_uppercase, k=3))}",
            moneda=mon, subtotal=100, gran_total=100, estatus=3,
            persona_pagos="QA PICK TRACKER",
            correo_facturas="qa-pick-trk@example.com", telefono_pagos="555",
            forma_pago="03", metodo_pago="PUE", uso_cfdi="G03",
            observaciones="QA PICK TRACKER smoke pedido con picking detalle",
            oc="QA-PICK-TRK-" + "".join(random.choices(string.ascii_uppercase, k=3)),
        )
        pd = PedidoDetalle.objects.create(
            pedido=ped, producto=prod, cantidad=10, precio_unitario=D("10.00"),
            subtotal=D("100.00"), total=D("100.00"),
        )
        pdt = PedidoDetalleTalla.objects.create(
            pedido_detalle=pd, talla=talla, variante=variante,
            cantidad=10, precio_unitario=D("10.00"), subtotal_talla=D("100.00"),
            lleva_bordado=True,
        )
        aeq(PedidoDetalleTalla.objects.filter(pedido_detalle__pedido=ped, lleva_bordado=True).count() >= 1,
            f"Pedido #{ped.pk} tiene al menos 1 PDT con lleva_bordado=True")
        pdt_id = pdt.pk
        cant = min(10, int(ex.disponible))

        # ---- Cliente APIClient auth (force_authenticate admin)
        client = APIClient()
        client.force_authenticate(user=admin)

        # ============================================================
        # TEST 4 — PICKING ONBOARDING (v2 Tracker de prendas)
        # ============================================================
        print()
        print("[TEST 4] WMS Picking — v2 Tracker de prendas + selector destino libre")
        print("-" * 70)

        # 4a) GET onboarding: shape NUEVO (almacenes_origen / almacenes_destino / header.tracker)
        url = (
            f"/api/v1/wms/pickings/onboarding/?pedido_id={ped.pk}"
            f"&almacen_origen_id={alm_origen.pk}&almacen_destino_id={alm_destino_1.pk}"
        )
        r = client.get(url)
        aeq(r.status_code == 200, f"GET picking onboarding {r.status_code}")
        dg = r.json()
        aeq("picking_detalle" in dg and "header" in dg and "pedidos" in dg, "shape Picking GET")
        aeq("almacenes_origen" in dg and isinstance(dg["almacenes_origen"], list),
            "campo nuevo 'almacenes_origen' presente (selector origen)")
        aeq("almacenes_destino" in dg and isinstance(dg["almacenes_destino"], list),
            "campo nuevo 'almacenes_destino' presente (selector destino)")
        aeq(len(dg["almacenes_origen"]) > 0 and len(dg["almacenes_destino"]) > 0,
            f"ambos selectores tienen opciones (origen={len(dg['almacenes_origen'])}, destino={len(dg['almacenes_destino'])})")
        aeq(
            any(a["id"] == alm_destino_2.pk for a in dg["almacenes_destino"]),
            "Selector destino incluye nuestro PROCESO (alm_destino_2) distinto a APARTADOS → usuario puede elegir destino alterno",
        )
        # header.tracker shape + numeros decimales normalizados strings
        header = dg["header"] or {}
        trk = header.get("tracker") or {}
        aeq(isinstance(trk.get("pct_asignado_pedido"), str),
            f"tracker.pct_asignado_pedido es string Decimal: {trk.get('pct_asignado_pedido')!r}")
        aeq(isinstance(trk.get("pct_surtido_pedido"), str),
            f"tracker.pct_surtido_pedido es string Decimal: {trk.get('pct_surtido_pedido')!r}")
        aeq(trk.get("total_prendas_pedido") == "10" or D(str(trk.get("total_prendas_pedido", "0"))) >= D("10"),
            f"tracker.total_prendas_pedido OK ({trk.get('total_prendas_pedido')})")
        # Maximo permitido por línea sigue siendo Decimal (fijamos que haya al menos una)
        row = next(
            (r for r in (dg.get("picking_detalle") or []) if r.get("pedido_detalle_talla") == pdt_id),
            None,
        )
        aeq(row is not None, f"existe picking_detalle row para PDT #{pdt_id}")
        maxp = row.get("maximo_picking_permitido", 0)
        try:
            _maxp_num = D(str(maxp))
        except Exception:
            _maxp_num = D("-1")
        aeq(_maxp_num > D("0"), f"maximo_picking_permitido > 0 (hay existencia en origen): {maxp}")
        print(f"      header.tracker = {trk}")

        # 4b) POST 201: Body TRADICIONAL pero AHORA ENVIAMOS almacen_destino EXPLICITO = APARTADOS
        body = {
            "pedido": ped.pk,
            "operador": admin.pk,
            "almacen": alm_origen.pk,
            "almacen_destino": alm_destino_1.pk,  # => APARTADOS (el de siempre, pero ahora explicito)
            "prioridad": "MEDIA",
            "tipo_picking": "ORDER_PICKING",
            "picking_detalle": [
                {"pedido_detalle_talla": pdt_id, "cantidad_asignada": cant,
                 "observaciones": "QA PICK TRACKER 4b destino=APARTADOS"}
            ],
        }
        r = client.post("/api/v1/wms/pickings/onboarding/", body, format="json")
        aeq(r.status_code == 201,
            f"POST pickings onboarding 4b (destino=APARTADOS explicito) → {r.status_code}")
        dp = r.json()
        aeq(dp.get("almacen_destino") == alm_destino_1.pk,
            f"Picking creado con almacen_destino_id = APARTADOS #{alm_destino_1.pk}")
        p = Picking.objects.get(pk=dp["id"])
        aeq(p.picking_detalle.count() == 1, "Detalle picking 4b bulk_create OK")
        # tracker: después de crear 1 picking con cantidad_asignada=cant,
        # el pct_asignado_pedido debe ser mayor a cero en el GET refrescado.
        r2 = client.get(url)
        dg2 = r2.json()
        trk2 = (dg2.get("header") or {}).get("tracker") or {}
        pct_asignado_2 = D(str(trk2.get("pct_asignado_pedido", "0")))
        aeq(pct_asignado_2 > D("0"),
            f"tracker.pct_asignado_pedido aumentó a {pct_asignado_2}% después de crear picking")
        print(f"      [refresco] header.tracker tras picking: {trk2}")

        # 4c) POST 201: POST con DESTINO ALTERNO = alm_destino_2 (PROCESO, NO APARTADOS)
        #    => validar que el usuario NO este obligado a APARTADOS (selector libre)
        #    Creamos un segundo picking parcial.
        remaining = max(0, 10 - cant)
        if remaining <= 0:
            # Si ya asignamos todo, actualizamos el setup para crear un segundo PDT
            pdt2 = PedidoDetalleTalla.objects.create(
                pedido_detalle=pd, talla=talla, variante=variante,
                cantidad=10, precio_unitario=D("10.00"), subtotal_talla=D("100.00"),
                lleva_bordado=True,
            )
            pdt2_id = pdt2.pk
            remaining2 = 5
        else:
            pdt2_id = pdt_id
            remaining2 = remaining
        body_c = {
            "pedido": ped.pk,
            "operador": admin.pk,
            "almacen": alm_origen.pk,
            "almacen_destino": alm_destino_2.pk,  # => PROCESO, NO APARTADOS
            "prioridad": "MEDIA",
            "tipo_picking": "ORDER_PICKING",
            "picking_detalle": [
                {"pedido_detalle_talla": pdt2_id, "cantidad_asignada": remaining2,
                 "observaciones": "QA PICK TRACKER 4c destino=PROCESO alterno"}
            ],
        }
        rc = client.post("/api/v1/wms/pickings/onboarding/", body_c, format="json")
        aeq(rc.status_code == 201,
            f"[4c] POST picking destino ALTERNO (PROCESO #{alm_destino_2.pk}) → {rc.status_code}: {rc.content[:400]!r}")
        dc = rc.json()
        aeq(dc.get("almacen_destino") == alm_destino_2.pk,
            f"[4c] Picking creado con almacen_destino PROCESO (no APARTADOS) ✅ selector libre OK")

        # 4d) POST 400: ORIGEN == DESTINO => ValidationError campo-específico
        body_d = dict(body_c)
        body_d["almacen"] = alm_destino_2.pk  # == almacen_destino
        body_d["picking_detalle"] = [
            {"pedido_detalle_talla": pdt2_id, "cantidad_asignada": 1,
             "observaciones": "QA PICK TRACKER 4d misma alma"}
        ]
        rd = client.post("/api/v1/wms/pickings/onboarding/", body_d, format="json")
        aeq(rd.status_code in {400, 409},
            f"[4d] POST origen==destino rechazado con {rd.status_code}: {rd.content[:400]!r}")
        ed = rd.json() if rd.content else {}
        aeq(
            (isinstance(ed, dict) and (
                "almacen_destino" in ed or "almacen" in ed or "detail" in ed
            )),
            f"[4d] Respuesta 400 trae mensaje campo-específico (almacen_destino o almacen): {ed!r}"
        )

        # 4e) POST 201: SIN almacen_destino en body => sugerencia default APARTADOS (alm_destino_1)
        #     Para asegurar que "falta destino y sugerir_apartados_por_defecto = alm_destino_1"
        body_e = {
            "pedido": ped.pk,
            "operador": admin.pk,
            "almacen": alm_origen.pk,
            # NO enviamos almacen_destino => backend debe sugerir APARTADOS = alm_destino_1
            # ya que coinciden empresa/sucursal + nombre APARTADOS
            # o si no existe APARTADOS en el catalogo general, falla con ValidationError.
            # Nuestro data setup renombro a "QA SMOKE APARTADOS PICK", pero la busqueda es por
            # nombre iexact == "APARTADOS". Por lo que el sugerir_apartados devuelve None y valida 400.
            # Asi que para dar un test robusto: renombramos temporalmente a "APARTADOS" puro.
            "prioridad": "MEDIA",
            "tipo_picking": "ORDER_PICKING",
            "picking_detalle": [
                {"pedido_detalle_talla": pdt2_id, "cantidad_asignada": 1,
                 "observaciones": "QA PICK TRACKER 4e sin destino"}
            ],
        }
        # Temporal rename de almacen destino 1 → "APARTADOS"
        old_nombre_1 = alm_destino_1.nombre
        alm_destino_1.nombre = "APARTADOS"
        alm_destino_1.save(update_fields=["nombre", "updated_at"])
        try:
            re = client.post("/api/v1/wms/pickings/onboarding/", body_e, format="json")
            aeq(re.status_code == 201,
                f"[4e] POST sin almacen_destino usa sugerencia default APARTADOS → {re.status_code}: {re.content[:400]!r}")
            de = re.json()
            aeq(de.get("almacen_destino") == alm_destino_1.pk,
                f"[4e] Picking creado sin body.destino pero con APARTADOS #{alm_destino_1.pk} como sugerencia default ✅")
        finally:
            alm_destino_1.nombre = old_nombre_1
            alm_destino_1.save(update_fields=["nombre", "updated_at"])

        print()
        print("[TEST 4] Resultados: 4a shape + 4b APARTADOS explicito + 4c PROCESO alterno + "
              "4d origen==destino 400 + 4e sugerencia APARTADOS default → TODOS PASARON ✅")

        # ============================================================
        # TEST 1/3 — OrdenBordado (fuma shape para confirmar no regression)
        # ============================================================
        print()
        print("[TEST 1/3] Produccion OrdenBordado onboarding GET (regression)")
        print("-" * 70)
        r_ob1 = client.get(f"/api/v1/produccion/orden-bordado/onboarding/?pedido_id={ped.pk}")
        aeq(r_ob1.status_code == 200, f"GET OB onboarding → {r_ob1.status_code}")
        dob = r_ob1.json()
        aeq(
            "preview" in dob and "pedidos" in dob and "operadores" in dob,
            "shape OB onboarding GET (preview, pedidos, operadores)",
        )
        folio_sug = (dob.get("preview") or {}).get("folio_ob_sugerido")
        aeq(folio_sug and isinstance(folio_sug, str),
            f"preview.folio_ob_sugerido presente sin consumir SerieFolio: {folio_sug!r}")
        sf_ob.refresh_from_db()
        aeq(sf_ob.folio_actual == 0, "SerieFolio OrdenBordado sigue en 0 (preview SIN consumo ✅)")
        print("      [OB GET] shape OK, SerieFolio intacta.")

        print()
        print("[TEST 3] Produccion OB POST + 409 anti-duplicado (regression)")
        print("-" * 70)
        sf_ob_before = SerieFolio.objects.get(pk=sf_ob.pk).folio_actual
        r_ob2 = client.post(
            "/api/v1/produccion/orden-bordado/onboarding/",
            {"pedido": ped.pk},
            format="json",
        )
        aeq(r_ob2.status_code == 201,
            f"[3a] POST OB onboarding → {r_ob2.status_code}: {r_ob2.content[:400]!r}")
        sf_ob_after = SerieFolio.objects.get(pk=sf_ob.pk).folio_actual
        aeq(sf_ob_after == sf_ob_before + 1,
            f"[3a] SerieFolio OB consume +1 (antes={sf_ob_before}, ahora={sf_ob_after})")
        # Intento duplicado mismo pedido
        r_ob3 = client.post(
            "/api/v1/produccion/orden-bordado/onboarding/",
            {"pedido": ped.pk},
            format="json",
        )
        aeq(r_ob3.status_code == 409,
            f"[3b] Reintento mismo pedido responde 409 Conflict: {r_ob3.status_code}")
        edob = r_ob3.json() if r_ob3.content else {}
        aeq("orden_bordado_existente" in edob,
            f"[3b] Respuesta 409 trae 'orden_bordado_existente': keys={list(edob.keys())}")
        exist_ref = (edob.get("orden_bordado_existente") or {}).get("folio")
        aeq(bool(exist_ref),
            f"[3b] orden_bordado_existente.folio presente: {exist_ref!r}")
        sf_ob_after_conflict = SerieFolio.objects.get(pk=sf_ob.pk).folio_actual
        aeq(sf_ob_after_conflict == sf_ob_after,
            f"[3b] SerieFolio NO consume en 409 (queda en {sf_ob_after_conflict}) ✅")

        print()
        print("=" * 70)
        print("TODOS LOS SMOKE TESTS PASARON ✅")
        print("=" * 70)
except AssertionError as ae:
    print()
    print("!!! SMOKE FAILED — rollback savepoint !!!")
    print(str(ae))
    transaction.savepoint_rollback(sid)
    sys.exit(1)
except Exception as e:
    print()
    print("!!! UNEXPECTED ERROR — rollback savepoint !!!")
    import traceback
    traceback.print_exc()
    transaction.savepoint_rollback(sid)
    sys.exit(2)
else:
    transaction.savepoint_commit(sid)
    print("(savepoint committed — datos QA de prueba se conservan en BD.)")

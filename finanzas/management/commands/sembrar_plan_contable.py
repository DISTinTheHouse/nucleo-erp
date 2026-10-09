"""Siembra un catálogo contable mínimo y su mapeo concepto -> cuenta.

Es un punto de partida para que contabilidad lo revise y lo ajuste, no un
catálogo definitivo: los códigos siguen la práctica común en México, pero cada
empresa tiene el suyo.

**No enciende la contabilización automática.** Eso es un acto aparte, sobre
``ParametrosContabilidad``, para poder sembrar y revisar con la generación
apagada.

    python manage.py sembrar_plan_contable --empresa 1
    python manage.py sembrar_plan_contable --empresa 1 --dry-run
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from finanzas.models import (
    CentroCosto,
    ConceptoContable,
    ConfiguracionContable,
    CuentaContable,
)
from nucleo.models import Empresa

# (código, nombre, tipo, concepto que resuelve)
CATALOGO = [
    ("1020", "Bancos", CuentaContable.CuentaTipo.ACTIVO, ConceptoContable.BANCOS),
    ("1120", "Clientes", CuentaContable.CuentaTipo.ACTIVO, ConceptoContable.CLIENTES),
    ("1150", "Inventario de materia prima", CuentaContable.CuentaTipo.ACTIVO, ConceptoContable.COMPRAS_INVENTARIO),
    ("1180", "Movimientos bancarios por identificar", CuentaContable.CuentaTipo.ACTIVO, ConceptoContable.CONTRAPARTIDA_MB_CARGO),
    ("1190", "IVA acreditable", CuentaContable.CuentaTipo.ACTIVO, ConceptoContable.IVA_ACREDITABLE),
    ("2110", "Proveedores", CuentaContable.CuentaTipo.PASIVO, ConceptoContable.PROVEEDORES),
    ("2120", "Nómina por pagar", CuentaContable.CuentaTipo.PASIVO, ConceptoContable.NOMINA_POR_PAGAR),
    ("2130", "Retenciones de nómina por pagar", CuentaContable.CuentaTipo.PASIVO, ConceptoContable.DEDUCCION_NOMINA),
    ("2170", "IVA trasladado", CuentaContable.CuentaTipo.PASIVO, ConceptoContable.IVA_TRASLADADO),
    ("2180", "Depósitos por identificar", CuentaContable.CuentaTipo.PASIVO, ConceptoContable.CONTRAPARTIDA_MB_ABONO),
    ("4100", "Ventas", CuentaContable.CuentaTipo.INGRESO, ConceptoContable.INGRESO_VENTAS),
    ("4150", "Devoluciones y rebajas sobre ventas", CuentaContable.CuentaTipo.INGRESO, ConceptoContable.DEVOLUCIONES_VENTAS),
    ("4160", "Descuentos sobre ventas", CuentaContable.CuentaTipo.INGRESO, ConceptoContable.DESCUENTO_VENTAS),
    ("5100", "Sueldos y salarios", CuentaContable.CuentaTipo.GASTO, ConceptoContable.PERCEPCION_NOMINA),
    ("5900", "Descuentos sobre compras", CuentaContable.CuentaTipo.GASTO, ConceptoContable.DESCUENTO_COMPRAS),
    ("6100", "Gastos de compra", CuentaContable.CuentaTipo.GASTO, ConceptoContable.GASTOS_COMPRA),
]

CENTRO_COSTO_DEFAULT = ("CC01", "General")


class Command(BaseCommand):
    help = "Siembra el catálogo contable mínimo y el mapeo concepto -> cuenta de una empresa."

    def add_arguments(self, parser):
        parser.add_argument("--empresa", type=int, required=True, help="id_empresa")
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Muestra lo que haría sin escribir nada.",
        )

    def handle(self, *args, **options):
        empresa_id = options["empresa"]
        dry_run = options["dry_run"]

        empresa = Empresa.objects.filter(pk=empresa_id).first()
        if empresa is None:
            raise CommandError(f"No existe la empresa {empresa_id}.")

        with transaction.atomic():
            cuentas_nuevas, configs_nuevas = self._sembrar(empresa, dry_run)
            if dry_run:
                transaction.set_rollback(True)

        prefijo = "[dry-run] " if dry_run else ""
        self.stdout.write(self.style.SUCCESS(
            f"{prefijo}{empresa.razon_social}: {cuentas_nuevas} cuentas y "
            f"{configs_nuevas} configuraciones nuevas."
        ))
        if not dry_run:
            self.stdout.write(
                "La contabilización automática sigue APAGADA. Revisa los asientos con "
                "GET /api/v1/finanzas/facturas/{id}/previsualizar-poliza/ y enciéndela "
                "desde /api/v1/finanzas/parametros-contabilidad/."
            )

    def _sembrar(self, empresa, dry_run):
        centro_costo, _ = CentroCosto.objects.get_or_create(
            empresa=empresa,
            codigo=CENTRO_COSTO_DEFAULT[0],
            defaults={"nombre": CENTRO_COSTO_DEFAULT[1]},
        )

        cuentas_nuevas = configs_nuevas = 0
        for codigo, nombre, tipo, concepto in CATALOGO:
            cuenta, creada = CuentaContable.objects.get_or_create(
                empresa=empresa,
                codigo=codigo,
                defaults={"nombre": nombre, "tipo": tipo, "acepta_movimientos": True},
            )
            cuentas_nuevas += int(creada)
            estado = "+" if creada else "="
            self.stdout.write(f"  {estado} {codigo} {nombre}")

            # Una configuración activa ya existente NO se pisa: puede apuntar a
            # la cuenta real de la empresa y este catálogo es sólo un arranque.
            if ConfiguracionContable.objects.filter(
                empresa=empresa, concepto=concepto, clave="", activo=True
            ).exists():
                continue
            ConfiguracionContable.objects.create(
                empresa=empresa,
                concepto=concepto,
                clave="",
                cuenta_contable=cuenta,
                centro_costo=centro_costo,
                descripcion=nombre,
            )
            configs_nuevas += 1

        return cuentas_nuevas, configs_nuevas

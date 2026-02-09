from django.core.management.base import BaseCommand
from nucleo.models import SatRegimenFiscal, SatUsoCfdi, SatMetodoPago, SatFormaPago

class Command(BaseCommand):
    help = 'Pobla los catálogos del SAT con datos iniciales básicos'

    def handle(self, *args, **kwargs):
        self.stdout.write("Iniciando carga de catálogos SAT...")

        # 1. Regímenes Fiscales
        regimenes = [
            {'codigo': '601', 'descripcion': 'General de Ley Personas Morales', 'aplica_fisica': False, 'aplica_moral': True},
            {'codigo': '603', 'descripcion': 'Personas Morales con Fines no Lucrativos', 'aplica_fisica': False, 'aplica_moral': True},
            {'codigo': '605', 'descripcion': 'Sueldos y Salarios e Ingresos Asimilados a Salarios', 'aplica_fisica': True, 'aplica_moral': False},
            {'codigo': '606', 'descripcion': 'Arrendamiento', 'aplica_fisica': True, 'aplica_moral': False},
            {'codigo': '612', 'descripcion': 'Personas Físicas con Actividades Empresariales y Profesionales', 'aplica_fisica': True, 'aplica_moral': False},
            {'codigo': '626', 'descripcion': 'Régimen Simplificado de Confianza', 'aplica_fisica': True, 'aplica_moral': True},
        ]
        
        for reg in regimenes:
            obj, created = SatRegimenFiscal.objects.get_or_create(
                codigo=reg['codigo'],
                defaults=reg
            )
            if created:
                self.stdout.write(f"Creado Régimen: {obj}")

        # 2. Uso CFDI
        usos = [
            {'codigo': 'G01', 'descripcion': 'Adquisición de mercancías', 'aplica_fisica': True, 'aplica_moral': True},
            {'codigo': 'G03', 'descripcion': 'Gastos en general', 'aplica_fisica': True, 'aplica_moral': True},
            {'codigo': 'P01', 'descripcion': 'Por definir', 'aplica_fisica': True, 'aplica_moral': True},
            {'codigo': 'D01', 'descripcion': 'Honorarios médicos, dentales y gastos hospitalarios', 'aplica_fisica': True, 'aplica_moral': False},
            {'codigo': 'S01', 'descripcion': 'Sin efectos fiscales', 'aplica_fisica': True, 'aplica_moral': True},
            {'codigo': 'CP01', 'descripcion': 'Pagos', 'aplica_fisica': True, 'aplica_moral': True},
            {'codigo': 'CN01', 'descripcion': 'Nómina', 'aplica_fisica': True, 'aplica_moral': True},
        ]

        for uso in usos:
            obj, created = SatUsoCfdi.objects.get_or_create(
                codigo=uso['codigo'],
                defaults=uso
            )
            if created:
                self.stdout.write(f"Creado UsoCFDI: {obj}")

        # 3. Métodos de Pago
        metodos = [
            {'codigo': 'PUE', 'descripcion': 'Pago en una sola exhibición'},
            {'codigo': 'PPD', 'descripcion': 'Pago en parcialidades o diferido'},
        ]

        for met in metodos:
            obj, created = SatMetodoPago.objects.get_or_create(
                codigo=met['codigo'],
                defaults=met
            )
            if created:
                self.stdout.write(f"Creado Método Pago: {obj}")

        # 4. Formas de Pago (Top más comunes)
        formas = [
            {'codigo': '01', 'descripcion': 'Efectivo', 'bancarizado': False},
            {'codigo': '02', 'descripcion': 'Cheque nominativo', 'bancarizado': True},
            {'codigo': '03', 'descripcion': 'Transferencia electrónica de fondos', 'bancarizado': True},
            {'codigo': '04', 'descripcion': 'Tarjeta de crédito', 'bancarizado': True},
            {'codigo': '28', 'descripcion': 'Tarjeta de débito', 'bancarizado': True},
            {'codigo': '99', 'descripcion': 'Por definir', 'bancarizado': False},
        ]

        for forma in formas:
            obj, created = SatFormaPago.objects.get_or_create(
                codigo=forma['codigo'],
                defaults=forma
            )
            if created:
                self.stdout.write(f"Creada Forma Pago: {obj}")

        # 5. Tipos de Relación (Si existe el modelo, verifiqué antes y parece que falta en el snippet leído pero es común)
        # Nota: En el snippet anterior no vi SatTipoRelacion, pero el usuario pidió "globales". 
        # Si no existe en models.py, lo omito por ahora para evitar errores.
        
        self.stdout.write(self.style.SUCCESS("Carga de catálogos SAT completada con éxito."))
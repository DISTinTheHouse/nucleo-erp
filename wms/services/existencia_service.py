from inventarios.models import Existencia


class ExistenciaService:
    @staticmethod
    def get_existencia(almacen, producto, producto_variante):
        filters = {
            "almacen": almacen,
        }

        if producto:
            filters["producto"] = producto
        else:
            filters["producto_variante"] = producto_variante

        return Existencia.objects.select_for_update().filter(**filters).first()

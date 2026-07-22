from inventarios.models import Existencia


class ExistenciaService:
    @staticmethod
    def get_existencia(almacen, producto, producto_variante):
        filters = {
            "almacen": almacen,
        }

        if producto_variante:
            filters["producto_variante"] = producto_variante
        else:
            filters["producto"] = producto

        return Existencia.objects.select_for_update().filter(**filters).first()

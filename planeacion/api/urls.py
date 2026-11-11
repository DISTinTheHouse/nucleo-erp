from django.urls import path

from planeacion.api.views import NecesidadesMateriaPrimaView, TableroRotacionView

urlpatterns = [
    path("tablero-rotacion/", TableroRotacionView.as_view(), name="planeacion-tablero-rotacion"),
    path("necesidades-materia-prima/", NecesidadesMateriaPrimaView.as_view(), name="planeacion-necesidades-mp"),
]

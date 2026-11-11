from django.urls import path

from planeacion.api.views import TableroRotacionView

urlpatterns = [
    path("tablero-rotacion/", TableroRotacionView.as_view(), name="planeacion-tablero-rotacion"),
]

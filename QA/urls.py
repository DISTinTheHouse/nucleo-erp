from django.urls import path
from . import views

urlpatterns = [
    path('', views.index, name='index_QA'),
    # PRODUCCION
    path('produccion_workspace/', views.produccion_workspace, name='produccion_workspace'),
    path('generar_orden_produccion/', views.generar_orden_produccion, name='generar_orden_produccion'),
]
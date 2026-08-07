from django.urls import path
from . import views

urlpatterns = [
    path('', views.index, name='index_QA'),
    # PRODUCCION
    path('produccion_workspace/', views.produccion_workspace, name='produccion_workspace'),
    path('generar_orden_produccion/', views.generar_orden_produccion, name='generar_orden_produccion'),
    path('rfid/recepciones/', views.recepcion_rfid_workspace, name='qa_recepcion_rfid_workspace'),
    path('browserprint/<str:filename>/', views.qa_browserprint_asset, name='qa_browserprint_asset'),
    path('imprimir_etiqueta/', views.imprimir_etiqueta_workspace, name='qa_imprimir_etiqueta_workspace'),
    path('imrpimir_etiqueta/', views.imprimir_etiqueta_workspace, name='qa_imrpimir_etiqueta_workspace'),
    path('imprimir_etiqueta/guardar/', views.qa_guardar_impresion_sku, name='qa_guardar_impresion_sku'),
    path('imprimir_orden_compra/', views.imprimir_orden_compra_workspace, name='qa_imprimir_orden_compra_workspace'),
    path('imrpimir_orden_compra/', views.imprimir_orden_compra_workspace, name='qa_imrpimir_orden_compra_workspace'),
    path('imprimir_orden_compra/<int:detalle_id>/guardar/', views.qa_guardar_impresion_oc, name='qa_guardar_impresion_oc'),
    path('scanner_rfid/', views.scanner_rfid_workspace, name='qa_scanner_rfid_workspace'),
    path('scanner_rfid/receive/', views.scanner_rfid_receive, name='qa_scanner_rfid_receive'),
    path('scanner_rfid/get/', views.scanner_rfid_get, name='qa_scanner_rfid_get'),
    path('scanner_rfid/clear/', views.scanner_rfid_clear, name='qa_scanner_rfid_clear'),
    path('scanner_rfid/stats/', views.scanner_rfid_stats, name='qa_scanner_rfid_stats'),
]

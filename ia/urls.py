from django.urls import path
from . import views

urlpatterns = [
    path('creator/', views.creator, name='ia_creator'),
]

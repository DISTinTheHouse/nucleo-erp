from rest_framework import serializers
from terceros.models import Proveedor, Cliente, DireccionCliente

class ClienteSerializer(serializers.ModelSerializer):
    class Meta:
        model = Cliente
        fields = "__all__"

class ProveedorSerializer(serializers.ModelSerializer):
    class Meta:
        model = Proveedor
        fields = "__all__"

class DireccionClienteSerializer(serializers.ModelSerializer):
    class Meta:
        model = DireccionCliente
        fields = "__all__"
from rest_framework import serializers
from terceros.models import Proveedor, Cliente, DireccionCliente
from nucleo.models import SatRegimenFiscal, SatUsoCfdi

class ClienteSerializer(serializers.ModelSerializer):
    razon_social = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    correo = serializers.EmailField(required=False, allow_blank=True, allow_null=True)
    telefono = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    giro_empresarial = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    sat_regimen_fiscal_codigo = serializers.CharField(write_only=True, required=False, allow_blank=True)
    sat_uso_cfdi_codigo = serializers.CharField(write_only=True, required=False, allow_blank=True)

    class Meta:
        model = Cliente
        fields = "__all__"
        read_only_fields = ["activo", "empresa", "vendedores"]

    def validate(self, attrs):
        rfc = (attrs.get("rfc") or "").strip().upper()
        if rfc:
            attrs["rfc"] = rfc
        request = self.context.get("request")
        empresa = getattr(getattr(request, "user", None), "empresa", None)
        if empresa and rfc:
            qs = Cliente.objects.filter(empresa=empresa, rfc__iexact=rfc, activo=True)
            if self.instance:
                qs = qs.exclude(pk=self.instance.pk)
            if qs.exists():
                raise serializers.ValidationError({"rfc": "Este cliente ya existe en tu empresa (RFC duplicado)."})
        codigo_regimen = attrs.pop("sat_regimen_fiscal_codigo", None)
        if codigo_regimen and not attrs.get("sat_regimen_fiscal"):
            try:
                attrs["sat_regimen_fiscal"] = SatRegimenFiscal.objects.get(codigo=str(codigo_regimen).strip())
            except SatRegimenFiscal.DoesNotExist:
                raise serializers.ValidationError({"sat_regimen_fiscal_codigo": "Régimen fiscal no encontrado"})
        codigo_uso = attrs.pop("sat_uso_cfdi_codigo", None)
        if codigo_uso and not attrs.get("sat_uso_cfdi"):
            try:
                attrs["sat_uso_cfdi"] = SatUsoCfdi.objects.get(codigo=str(codigo_uso).strip())
            except SatUsoCfdi.DoesNotExist:
                raise serializers.ValidationError({"sat_uso_cfdi_codigo": "Uso CFDI no encontrado"})
        if "telefono" in attrs and not attrs["telefono"]:
            attrs["telefono"] = ""
        if "correo" in attrs and not attrs["correo"]:
            attrs["correo"] = ""
        if "giro_empresarial" in attrs and not attrs["giro_empresarial"]:
            attrs["giro_empresarial"] = ""
        if "razon_social" in attrs and not attrs["razon_social"]:
            attrs["razon_social"] = ""
        return attrs

class ProveedorSerializer(serializers.ModelSerializer):
    def validate_moneda(self, moneda):
        # Global (sin empresa) o privada de la empresa del proveedor.
        if moneda is None or moneda.empresa_id is None:
            return moneda
        if self.instance is not None:
            empresa_id = self.instance.empresa_id
        else:
            empresa_id = getattr(getattr(self.context.get("request"), "user", None), "empresa_id", None)
        if moneda.empresa_id != empresa_id:
            raise serializers.ValidationError("La moneda no está disponible para la empresa del proveedor.")
        return moneda

    class Meta:
        model = Proveedor
        fields = "__all__"
        # ``empresa`` la asigna el servidor (``ProveedorViewSet.perform_create``).
        read_only_fields = ["activo", "empresa"]

class DireccionClienteSerializer(serializers.ModelSerializer):
    class Meta:
        model = DireccionCliente
        fields = "__all__"
        read_only_fields = ["activo"]

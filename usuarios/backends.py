from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend
from rest_framework.authentication import TokenAuthentication

class BearerTokenAuthentication(TokenAuthentication):
    """
    Extiende TokenAuthentication para usar la palabra clave 'Bearer'
    en lugar de 'Token' en el header de autorización.
    Esto mejora la compatibilidad con clientes estándar como Postman y Next.js.
    """
    keyword = 'Bearer'

class EmailBackend(ModelBackend):
    """
    Autentica contra la base de datos de settings.AUTH_USER_MODEL.
    Usa el campo 'email' en lugar de 'username'.
    """
    def authenticate(self, request, username=None, password=None, **kwargs):
        # Si username es None, intentamos obtenerlo de kwargs (DRF a veces lo pasa como 'email' o 'username')
        if username is None:
            username = kwargs.get('email')
        
        # Si aún es None, retornamos
        if username is None:
            return None
            
        UserModel = get_user_model()
        try:
            # Buscamos el usuario por email (case insensitive)
            user = UserModel.objects.get(email__iexact=username)
        except UserModel.DoesNotExist:
            return None
        else:
            if user.check_password(password) and self.user_can_authenticate(user):
                return user
        return None

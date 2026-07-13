# app/api/deps.py

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from typing import List, Dict, Any

from app.core.config import settings
from app.core.auth import oauth2_scheme, _aplicar_permiso_remoto
from app.db.queries import execute_auth_query, execute_query
# --- Importar los schemas necesarios ---
from app.schemas.auth import TokenPayload
from app.schemas.usuario import UsuarioReadWithRoles # <<< Importar schema de usuario
from app.schemas.rol import RolRead # <<< Importar schema de rol
# --- Fin importación schemas ---
from app.services.usuario_service import UsuarioService

import logging
logger = logging.getLogger(__name__)

# Excepciones (sin cambios)
credentials_exception = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="No se pudieron validar las credenciales",
    headers={"WWW-Authenticate": "Bearer"},
)
inactive_user_exception = HTTPException(
    status_code=status.HTTP_403_FORBIDDEN,
    detail="Usuario inactivo",
)
forbidden_exception = HTTPException(
    status_code=status.HTTP_403_FORBIDDEN,
    detail="Permisos insuficientes",
)

# get_current_user_data (sin cambios)
async def get_current_user_data(token: str = Depends(oauth2_scheme)) -> Dict[str, Any]:
    """
    Decodifica el token, obtiene el nombre de usuario y lo devuelve.
    No accede a la base de datos aquí para optimizar.
    Lanza excepción si el token es inválido o ha expirado.
    """
    try:
        payload = jwt.decode(
            token,
            settings.SECRET_KEY,
            algorithms=[settings.ALGORITHM]
        )
        username: str = payload.get("sub")
        if username is None:
            logger.warning("Token JWT inválido: falta 'sub'.")
            raise credentials_exception
        return payload
    except JWTError as e:
        logger.warning(f"Error de validación JWT: {e}")
        raise credentials_exception

# --- get_current_active_user CORREGIDO ---
async def get_current_active_user(
    payload: Dict[str, Any] = Depends(get_current_user_data)
) -> UsuarioReadWithRoles: # <<< Tipo de retorno es UsuarioReadWithRoles
    """
    Dependencia principal: Obtiene los datos completos del usuario activo desde la BD
    basado en el nombre de usuario del token, añade sus roles (como objetos RolRead)
    y devuelve una instancia del schema UsuarioReadWithRoles.
    """
    username = payload.get("sub")

    try:
        # Obtener datos básicos del usuario como diccionario
        user_query = """
        SELECT usuario_id, nombre_usuario, correo, nombre, apellido, es_activo,
               fecha_creacion, fecha_ultimo_acceso, correo_confirmado,
               codigo_trabajador_externo, origen_datos
        FROM usuario
        WHERE nombre_usuario = ? AND es_eliminado = 0
        """
        user_dict = execute_auth_query(user_query, (username,)) # Asume que devuelve dict o None

        if not user_dict:
            logger.warning(f"Usuario '{username}' del token válido no encontrado en BD (o eliminado).")
            raise credentials_exception

        if not user_dict.get('es_activo'):
            logger.warning(f"Usuario '{username}' autenticado pero inactivo.")
            raise inactive_user_exception

        user_data_cliente = None
        if user_dict.get("origen_datos") == "cliente":
            from app.db.queries import SELECT_CLIENTE_USER_DATA
            user_data_cliente = execute_auth_query(SELECT_CLIENTE_USER_DATA, (username,))
        _aplicar_permiso_remoto(user_dict, username, user_data_cliente)

        # Obtener roles del usuario usando el servicio (devuelve List[Dict])
        roles_list: List[RolRead] = [] # Inicializar lista para objetos RolRead
        try:
            # roles_dict_list será del tipo List[Dict]
            roles_dict_list: List[Dict] = await UsuarioService.obtener_roles_de_usuario(user_dict['usuario_id'])
            logger.debug(f"Roles (dicts) obtenidos para usuario ID {user_dict['usuario_id']}: {roles_dict_list}")

            # --- Convertir List[Dict] a List[RolRead] ---
            for rol_dict in roles_dict_list:
                try:
                    # Crear instancia de RolRead desde cada diccionario
                    roles_list.append(RolRead(**rol_dict))
                except Exception as rol_parse_error:
                    # Loggear error si un diccionario de rol no es válido
                    logger.error(f"Error parseando diccionario de rol a RolRead: {rol_dict}. Error: {rol_parse_error}", exc_info=True)
                    # Decidir si continuar sin este rol o fallar. Por seguridad, fallamos.
                    raise HTTPException(
                        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                        detail="Error interno al procesar datos de roles del usuario."
                    )
            # --- FIN CONVERSIÓN ---

            logger.debug(f"Roles convertidos a List[RolRead] para usuario ID {user_dict['usuario_id']}: {[r.nombre for r in roles_list]}")


        except Exception as role_error:
            # Captura errores de UsuarioService.obtener_roles_de_usuario o de la conversión
            logger.error(f"Error obteniendo/procesando roles para usuario ID {user_dict['usuario_id']}: {role_error}", exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Error interno al obtener o procesar roles del usuario."
            )

        # --- Construir y devolver la instancia Pydantic ---
        try:
            # Crear la instancia usando el diccionario de datos y la LISTA DE OBJETOS RolRead
            usuario_pydantic = UsuarioReadWithRoles(**user_dict, roles=roles_list)
            logger.debug(f"Usuario activo '{username}' (ID: {usuario_pydantic.usuario_id}) construido como objeto Pydantic.")
            return usuario_pydantic # <<< Devolver el objeto Pydantic

        except Exception as pydantic_error:
            # Error si los datos del diccionario no coinciden con el schema
            logger.error(f"Error creando instancia Pydantic UsuarioReadWithRoles para '{username}': {pydantic_error}", exc_info=True)
            logger.error(f"Datos del usuario (dict): {user_dict}")
            logger.error(f"Roles obtenidos y convertidos (list[RolRead]): {roles_list}") # Loggear la lista convertida
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Error interno al procesar datos del usuario."
            )

    except HTTPException as e:
        # Re-lanzar excepciones HTTP ya manejadas (credentials, inactive, etc.)
        raise e
    except Exception as e:
        # Capturar cualquier otro error inesperado
        logger.error(f"Error inesperado obteniendo usuario activo '{username}': {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error interno al verificar el usuario."
        )


# --- RoleChecker (sin cambios respecto a la versión anterior, ya espera UsuarioReadWithRoles) ---

class RoleChecker:
    """
    Clase para crear dependencias que verifican roles específicos.
    
    El usuario con rol "SuperAdministrador" tiene acceso automático a todo,
    sin necesidad de verificar roles específicos.
    """
    def __init__(self, allowed_roles: List[str]):
        self.allowed_roles = allowed_roles

    async def __call__(self,
        # <<< Espera UsuarioReadWithRoles
        current_user: UsuarioReadWithRoles = Depends(get_current_active_user)
    ):
        """
        Verifica si alguno de los roles del usuario actual está en la lista de roles permitidos.
        Si el usuario tiene el rol "SuperAdministrador", se le otorga acceso automático.
        """
        # <<< Acceder a la lista de objetos RolRead
        user_roles_objects = current_user.roles
        # <<< Extraer los nombres de los roles de los objetos
        user_role_names = [role.nombre for role in user_roles_objects]

        logger.debug(f"Verificando roles. Usuario: {current_user.nombre_usuario}, Roles: {user_role_names}, Roles requeridos: {self.allowed_roles}")

        # 🚀 SUPERADMINISTRADOR: Acceso automático a todo
        # Verifica por rol "SuperAdministrador" o por nombre de usuario "superadmin" (case-insensitive)
        if "SuperAdministrador" in user_role_names or current_user.nombre_usuario.lower() == "superadmin":
            logger.debug(f"Acceso permitido automáticamente para SuperAdministrador '{current_user.nombre_usuario}'.")
            return

        # Comprobar si hay alguna intersección entre los nombres de roles del usuario y los permitidos
        if not any(role_name in self.allowed_roles for role_name in user_role_names):
            logger.warning(f"Acceso denegado para usuario '{current_user.nombre_usuario}'. Roles: {user_role_names}. Roles requeridos: {self.allowed_roles}")
            raise forbidden_exception

        logger.debug(f"Acceso permitido para usuario '{current_user.nombre_usuario}' basado en roles.")
        # No es necesario devolver nada explícitamente si solo se usa para autorización.
"""
Endpoints para Marcaciones Remotas Seguras.

Permite verificar/registrar dispositivos autorizados y registrar marcaciones
con doble persistencia (legacy + auditoría moderna).
"""

from typing import Dict, Optional

from fastapi import APIRouter, HTTPException, Depends, status, Query, Request

from app.schemas.marcaciones import (
    VerificarDispositivoResponse,
    RegistrarDispositivoRequest,
    MarcacionRemotaCreate,
)
from app.schemas.usuario import UsuarioReadWithRoles
from app.api.deps import get_current_active_user
from app.services.marcaciones_service import MarcacionesService
from app.core.exceptions import CustomException
from app.core.logging_config import get_logger

logger = get_logger(__name__)
router = APIRouter()


def _validar_permiso_remoto(current_user: UsuarioReadWithRoles) -> None:
    if not current_user.permiso_remoto:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No autorizado para realizar marcaciones remotas.",
        )


def _obtener_codigo_trabajador(current_user: UsuarioReadWithRoles) -> str:
    codigo = (current_user.codigo_trabajador_externo or "").strip()
    if not codigo:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Usuario no tiene código de trabajador asociado. Contacte al administrador.",
        )
    return codigo


@router.get(
    "/estado-hoy",
    summary="Estado de marcaciones del día",
    description="Retorna las horas de marcación del día actual para los tipos 01-04 del trabajador autenticado.",
)
async def estado_hoy(
    current_user: UsuarioReadWithRoles = Depends(get_current_active_user),
) -> Dict[str, Optional[str]]:
    codigo_trabajador = _obtener_codigo_trabajador(current_user)

    try:
        return await MarcacionesService.obtener_estado_hoy(codigo_trabajador)
    except HTTPException:
        raise
    except CustomException as ce:
        raise HTTPException(status_code=ce.status_code, detail=ce.detail)
    except Exception as e:
        logger.exception(f"Error obteniendo estado de marcaciones del día: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error al obtener el estado de marcaciones del día.",
        )


@router.get(
    "/verificar-dispositivo",
    response_model=VerificarDispositivoResponse,
    summary="Verificar dispositivo registrado",
    description="Comprueba si el token del dispositivo está activo para el trabajador autenticado.",
)
async def verificar_dispositivo(
    token_dispositivo: str = Query(..., min_length=1, description="Token único del dispositivo móvil"),
    current_user: UsuarioReadWithRoles = Depends(get_current_active_user),
):
    _validar_permiso_remoto(current_user)
    codigo_trabajador = _obtener_codigo_trabajador(current_user)

    try:
        registrado = await MarcacionesService.verificar_dispositivo(
            codigo_trabajador=codigo_trabajador,
            token_dispositivo=token_dispositivo,
        )
        return {"registrado": registrado}
    except HTTPException:
        raise
    except CustomException as ce:
        raise HTTPException(status_code=ce.status_code, detail=ce.detail)
    except Exception as e:
        logger.exception(f"Error verificando dispositivo: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error al verificar el dispositivo.",
        )


@router.post(
    "/registrar-dispositivo",
    summary="Registrar o activar dispositivo",
    description="Desactiva dispositivos previos del trabajador y registra el nuevo token como activo.",
)
async def registrar_dispositivo(
    body: RegistrarDispositivoRequest,
    current_user: UsuarioReadWithRoles = Depends(get_current_active_user),
):
    _validar_permiso_remoto(current_user)
    codigo_trabajador = _obtener_codigo_trabajador(current_user)

    try:
        await MarcacionesService.registrar_dispositivo(
            codigo_trabajador=codigo_trabajador,
            token_dispositivo=body.token_dispositivo,
            modelo=body.modelo_dispositivo,
        )
        return {"mensaje": "Dispositivo registrado exitosamente."}
    except HTTPException:
        raise
    except CustomException as ce:
        raise HTTPException(status_code=ce.status_code, detail=ce.detail)
    except Exception as e:
        logger.exception(f"Error registrando dispositivo: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error al registrar el dispositivo.",
        )


@router.post(
    "/marcar",
    status_code=status.HTTP_201_CREATED,
    summary="Registrar marcación remota",
    description="Registra la marcación en tablas legacy y de auditoría moderna.",
)
async def marcar(
    body: MarcacionRemotaCreate,
    request: Request,
    current_user: UsuarioReadWithRoles = Depends(get_current_active_user),
):
    _validar_permiso_remoto(current_user)
    codigo_trabajador = _obtener_codigo_trabajador(current_user)
    ip_cliente = request.client.host if request.client else ""

    try:
        await MarcacionesService.registrar_marcacion(
            codigo_trabajador=codigo_trabajador,
            datos=body,
            ip_cliente=ip_cliente,
        )
        return {"mensaje": "Marcación registrada exitosamente."}
    except HTTPException:
        raise
    except CustomException as ce:
        raise HTTPException(status_code=ce.status_code, detail=ce.detail)
    except Exception as e:
        logger.exception(f"Error registrando marcación: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error al registrar la marcación.",
        )

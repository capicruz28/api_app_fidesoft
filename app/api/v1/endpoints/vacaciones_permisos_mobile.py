# app/api/v1/endpoints/vacaciones_permisos_mobile.py
"""
Endpoints móviles para el Sistema de Gestión de Vacaciones y Permisos.

Este módulo proporciona endpoints específicos para la aplicación móvil (Flutter),
incluyendo funcionalidades para empleados y aprobadores.
"""

from fastapi import APIRouter, HTTPException, Depends, status, Query, Request
from typing import Optional, List, Dict, Any
from datetime import date

# Schemas
from app.schemas.vacaciones_permisos import (
    SolicitudCreate, SolicitudUpdate, SolicitudRead, SolicitudReadFull,
    AprobacionRead, AprobacionUpdate, AprobacionResponse,
    NotificacionRead, PaginatedNotificacionResponse,
    SaldoVacacionesRead, CatalogosResponse, CatalogoItem, CatalogoPermisoItem,
    PaginatedSolicitudResponse, TrabajadorRead, PaginatedTrabajadorResponse,
    BoletaPagoResponse, BoletasPagoResponse, CertificadoCTSResponse, CertificadosCTSResponse,
    DocumentosEmpresaResponse, AvisosEmpresaResponse
)

# Servicios
from app.services.vacaciones_permisos_service import VacacionesPermisosService

# Dependencias
from app.api.deps import get_current_active_user
from app.schemas.usuario import UsuarioReadWithRoles

# Excepciones
from app.core.exceptions import CustomException

# Logging
from app.core.logging_config import get_logger

logger = get_logger(__name__)
router = APIRouter()


def _map_tipo_permiso_catalogo(item: Dict[str, Any]) -> CatalogoPermisoItem:
    """
    Serializa un registro de vw_mconfa00 hacia CatalogoPermisoItem.
    El campo tiempo (ctiempo en BD) es obligatorio para el frontend (D/H).
    """
    tiempo_raw = item.get('tiempo') or item.get('ctiempo')
    return CatalogoPermisoItem(
        codigo=str(item.get('codigo', '')).strip(),
        descripcion=str(item.get('descripcion', '')).strip(),
        tiempo=str(tiempo_raw).strip().upper() if tiempo_raw else 'D',
    )


def obtener_codigo_trabajador(current_user: UsuarioReadWithRoles) -> str:
    """
    Obtiene el código de trabajador del usuario actual.
    
    Busca en codigo_trabajador_externo del usuario.
    Si no existe, lanza una excepción.
    """
    # Obtener código de trabajador desde la BD
    from app.db.queries import execute_auth_query
    
    query = """
        SELECT codigo_trabajador_externo
        FROM usuario
        WHERE usuario_id = ? AND es_eliminado = 0
    """
    
    resultado = execute_auth_query(query, (current_user.usuario_id,))
    
    if not resultado or not resultado.get('codigo_trabajador_externo'):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Usuario no tiene código de trabajador asociado. Contacte al administrador."
        )
    
    return resultado['codigo_trabajador_externo'].strip()


# ============================================
# ENDPOINTS DE SOLICITUDES
# ============================================

@router.post(
    "/solicitar",
    response_model=SolicitudReadFull,
    status_code=status.HTTP_201_CREATED,
    summary="Crear nueva solicitud",
    description="Crea una nueva solicitud de vacaciones o permiso"
)
async def crear_solicitud(
    solicitud_data: SolicitudCreate,
    current_user: UsuarioReadWithRoles = Depends(get_current_active_user)
):
    """Crea una nueva solicitud de vacaciones o permiso"""
    try:
        codigo_trabajador = obtener_codigo_trabajador(current_user)
        
        # Asegurar que el código de trabajador coincida
        if solicitud_data.codigo_trabajador != codigo_trabajador:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No puede crear solicitudes para otros trabajadores"
            )
        
        resultado = await VacacionesPermisosService.crear_solicitud(
            solicitud_data=solicitud_data,
            usuario_registro=current_user.nombre_usuario
        )
        
        logger.info(f"Solicitud creada por {current_user.nombre_usuario}: {resultado.get('id_solicitud')}")
        return resultado
        
    except HTTPException:
        raise
    except CustomException as ce:
        logger.warning(f"Error de negocio al crear solicitud: {ce.detail}")
        raise HTTPException(status_code=ce.status_code, detail=ce.detail)
    except Exception as e:
        logger.exception(f"Error creando solicitud: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error al crear solicitud"
        )


@router.get(
    "/mis-solicitudes",
    response_model=PaginatedSolicitudResponse,
    summary="Obtener mis solicitudes",
    description="Obtiene el historial de solicitudes del usuario autenticado"
)
async def obtener_mis_solicitudes(
    page: int = Query(1, ge=1, description="Número de página"),
    limit: int = Query(20, ge=1, le=100, description="Elementos por página"),
    estado: Optional[str] = Query(None, description="Filtrar por estado (P, A, R, N)"),
    tipo_solicitud: Optional[str] = Query(None, description="Filtrar por tipo (V, P)"),
    current_user: UsuarioReadWithRoles = Depends(get_current_active_user)
):
    """Obtiene las solicitudes del usuario autenticado"""
    try:
        codigo_trabajador = obtener_codigo_trabajador(current_user)
        
        resultado = await VacacionesPermisosService.listar_solicitudes(
            codigo_trabajador=codigo_trabajador,
            estado=estado,
            tipo_solicitud=tipo_solicitud,
            page=page,
            limit=limit
        )
        
        return resultado
        
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Error obteniendo solicitudes: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error al obtener solicitudes"
        )


@router.get(
    "/solicitud/{id_solicitud}",
    response_model=SolicitudReadFull,
    summary="Obtener detalle de solicitud",
    description="Obtiene el detalle completo de una solicitud"
)
async def obtener_solicitud(
    id_solicitud: int,
    current_user: UsuarioReadWithRoles = Depends(get_current_active_user)
):
    """Obtiene el detalle de una solicitud"""
    try:
        codigo_trabajador = obtener_codigo_trabajador(current_user)
        
        solicitud = await VacacionesPermisosService.obtener_solicitud(id_solicitud)
        
        # Verificar que el usuario tiene acceso (es el solicitante o un aprobador)
        es_solicitante = solicitud['codigo_trabajador'] == codigo_trabajador
        es_aprobador = any(
            a['codigo_trabajador_aprueba'] == codigo_trabajador
            for a in solicitud.get('aprobaciones', [])
        )
        
        if not (es_solicitante or es_aprobador):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No tiene acceso a esta solicitud"
            )
        
        return solicitud
        
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Error obteniendo solicitud: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error al obtener solicitud"
        )


@router.put(
    "/solicitud/{id_solicitud}",
    response_model=SolicitudReadFull,
    summary="Actualizar solicitud",
    description="Actualiza una solicitud pendiente"
)
async def actualizar_solicitud(
    id_solicitud: int,
    solicitud_data: SolicitudUpdate,
    current_user: UsuarioReadWithRoles = Depends(get_current_active_user)
):
    """Actualiza una solicitud pendiente"""
    try:
        codigo_trabajador = obtener_codigo_trabajador(current_user)
        
        # Verificar que es el solicitante
        solicitud = await VacacionesPermisosService.obtener_solicitud(id_solicitud)
        
        if solicitud['codigo_trabajador'] != codigo_trabajador:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Solo puede actualizar sus propias solicitudes"
            )
        
        resultado = await VacacionesPermisosService.actualizar_solicitud(
            id_solicitud=id_solicitud,
            solicitud_data=solicitud_data,
            usuario_modificacion=current_user.nombre_usuario
        )
        
        return resultado
        
    except HTTPException:
        raise
    except CustomException as ce:
        logger.warning(f"Error de negocio al actualizar solicitud: {ce.detail}")
        raise HTTPException(status_code=ce.status_code, detail=ce.detail)
    except Exception as e:
        logger.exception(f"Error actualizando solicitud: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error al actualizar solicitud"
        )


@router.delete(
    "/solicitud/{id_solicitud}",
    response_model=SolicitudReadFull,
    summary="Anular solicitud",
    description="Anula una solicitud pendiente"
)
async def anular_solicitud(
    id_solicitud: int,
    motivo_anulacion: str = Query(..., description="Motivo de la anulación"),
    current_user: UsuarioReadWithRoles = Depends(get_current_active_user)
):
    """Anula una solicitud pendiente"""
    try:
        codigo_trabajador = obtener_codigo_trabajador(current_user)
        
        # Verificar que es el solicitante
        solicitud = await VacacionesPermisosService.obtener_solicitud(id_solicitud)
        
        if solicitud['codigo_trabajador'] != codigo_trabajador:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Solo puede anular sus propias solicitudes"
            )
        
        resultado = await VacacionesPermisosService.anular_solicitud(
            id_solicitud=id_solicitud,
            motivo_anulacion=motivo_anulacion,
            usuario_anulacion=current_user.nombre_usuario
        )
        
        return resultado
        
    except HTTPException:
        raise
    except CustomException as ce:
        logger.warning(f"Error de negocio al anular solicitud: {ce.detail}")
        raise HTTPException(status_code=ce.status_code, detail=ce.detail)
    except Exception as e:
        logger.exception(f"Error anulando solicitud: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error al anular solicitud"
        )


# ============================================
# ENDPOINTS DE APROBACIONES
# ============================================

@router.get(
    "/pendientes-aprobar",
    response_model=List[Dict],
    summary="Obtener solicitudes pendientes de aprobar",
    description="Obtiene las solicitudes que el usuario debe aprobar"
)
async def obtener_pendientes_aprobacion(
    current_user: UsuarioReadWithRoles = Depends(get_current_active_user)
):
    """Obtiene las solicitudes pendientes de aprobación"""
    try:
        codigo_trabajador = obtener_codigo_trabajador(current_user)
        
        resultado = await VacacionesPermisosService.obtener_solicitudes_pendientes_aprobacion(
            codigo_trabajador_aprobador=codigo_trabajador
        )
        
        return resultado
        
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Error obteniendo pendientes: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error al obtener solicitudes pendientes"
        )


@router.post(
    "/aprobar/{id_solicitud}",
    response_model=AprobacionResponse,
    summary="Aprobar solicitud",
    description="Aprueba una solicitud en el nivel correspondiente"
)
async def aprobar_solicitud(
    id_solicitud: int,
    aprobacion_data: AprobacionUpdate,
    current_user: UsuarioReadWithRoles = Depends(get_current_active_user),
    request: Request = None
):
    """Aprueba una solicitud"""
    try:
        codigo_trabajador = obtener_codigo_trabajador(current_user)
        
        # Obtener aprobaciones para encontrar el ID de aprobación
        aprobaciones = await VacacionesPermisosService.obtener_aprobaciones(id_solicitud)
        aprobacion_pendiente = next(
            (a for a in aprobaciones if a['codigo_trabajador_aprueba'] == codigo_trabajador and a['estado'] == 'P'),
            None
        )
        
        if not aprobacion_pendiente:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="No tiene una aprobación pendiente para esta solicitud"
            )
        
        # Obtener IP del dispositivo
        if request:
            ip_dispositivo = request.client.host if request.client else None
            aprobacion_data.ip_dispositivo = ip_dispositivo
        
        # Convertir a diccionario y establecer estado automáticamente como "A" (Aprobado)
        aprobacion_dict = aprobacion_data.dict()
        aprobacion_dict['estado'] = "A"
        
        resultado = await VacacionesPermisosService.aprobar_solicitud(
            id_solicitud=id_solicitud,
            id_aprobacion=aprobacion_pendiente['id_aprobacion'],
            aprobacion_data=aprobacion_dict,
            codigo_trabajador_aprobador=codigo_trabajador
        )
        
        return resultado
        
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Error aprobando solicitud: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error al aprobar solicitud"
        )


@router.post(
    "/rechazar/{id_solicitud}",
    response_model=AprobacionResponse,
    summary="Rechazar solicitud",
    description="Rechaza una solicitud en el nivel correspondiente"
)
async def rechazar_solicitud(
    id_solicitud: int,
    aprobacion_data: AprobacionUpdate,
    current_user: UsuarioReadWithRoles = Depends(get_current_active_user),
    request: Request = None
):
    """Rechaza una solicitud"""
    try:
        codigo_trabajador = obtener_codigo_trabajador(current_user)
        
        # Obtener aprobaciones
        aprobaciones = await VacacionesPermisosService.obtener_aprobaciones(id_solicitud)
        aprobacion_pendiente = next(
            (a for a in aprobaciones if a['codigo_trabajador_aprueba'] == codigo_trabajador and a['estado'] == 'P'),
            None
        )
        
        if not aprobacion_pendiente:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="No tiene una aprobación pendiente para esta solicitud"
            )
        
        # Validar que tiene observación
        if not aprobacion_data.observacion:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Debe proporcionar una observación al rechazar"
            )
        
        # Obtener IP del dispositivo
        if request:
            ip_dispositivo = request.client.host if request.client else None
            aprobacion_data.ip_dispositivo = ip_dispositivo
        
        # Convertir a diccionario y establecer estado automáticamente como "R" (Rechazado)
        aprobacion_dict = aprobacion_data.dict()
        aprobacion_dict['estado'] = "R"
        
        resultado = await VacacionesPermisosService.rechazar_solicitud(
            id_solicitud=id_solicitud,
            id_aprobacion=aprobacion_pendiente['id_aprobacion'],
            aprobacion_data=aprobacion_dict,
            codigo_trabajador_aprobador=codigo_trabajador
        )
        
        return resultado
        
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Error rechazando solicitud: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error al rechazar solicitud"
        )


@router.get(
    "/aprobaciones/{id_solicitud}",
    response_model=List[AprobacionRead],
    summary="Obtener aprobaciones de una solicitud",
    description="Obtiene todas las aprobaciones de una solicitud"
)
async def obtener_aprobaciones(
    id_solicitud: int,
    current_user: UsuarioReadWithRoles = Depends(get_current_active_user)
):
    """Obtiene las aprobaciones de una solicitud"""
    try:
        codigo_trabajador = obtener_codigo_trabajador(current_user)
        
        # Verificar acceso
        solicitud = await VacacionesPermisosService.obtener_solicitud(id_solicitud)
        es_solicitante = solicitud['codigo_trabajador'] == codigo_trabajador
        es_aprobador = any(
            a['codigo_trabajador_aprueba'] == codigo_trabajador
            for a in solicitud.get('aprobaciones', [])
        )
        
        if not (es_solicitante or es_aprobador):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No tiene acceso a esta solicitud"
            )
        
        aprobaciones = await VacacionesPermisosService.obtener_aprobaciones(id_solicitud)
        return aprobaciones
        
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Error obteniendo aprobaciones: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error al obtener aprobaciones"
        )


# ============================================
# ENDPOINTS DE NOTIFICACIONES
# ============================================

@router.get(
    "/notificaciones",
    response_model=PaginatedNotificacionResponse,
    summary="Obtener notificaciones",
    description="Obtiene las notificaciones del usuario con paginación"
)
async def obtener_notificaciones(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    leido: Optional[str] = Query(None, description="Filtrar por estado de lectura (S, N)"),
    current_user: UsuarioReadWithRoles = Depends(get_current_active_user)
):
    """Obtiene las notificaciones del usuario"""
    try:
        codigo_trabajador = obtener_codigo_trabajador(current_user)
        
        resultado = await VacacionesPermisosService.obtener_notificaciones(
            codigo_trabajador=codigo_trabajador,
            leido=leido,
            page=page,
            limit=limit
        )
        
        return resultado
        
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Error obteniendo notificaciones: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error al obtener notificaciones"
        )


@router.put(
    "/notificaciones/{id_notificacion}/leer",
    response_model=Dict,
    summary="Marcar notificación como leída",
    description="Marca una notificación como leída"
)
async def marcar_notificacion_leida(
    id_notificacion: int,
    current_user: UsuarioReadWithRoles = Depends(get_current_active_user)
):
    """Marca una notificación como leída"""
    try:
        resultado = await VacacionesPermisosService.marcar_notificacion_leida(id_notificacion)
        return resultado
        
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Error marcando notificación: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error al marcar notificación como leída"
        )


@router.get(
    "/notificaciones/no-leidas",
    response_model=Dict,
    summary="Contador de notificaciones no leídas",
    description="Obtiene la cantidad de notificaciones no leídas"
)
async def contar_notificaciones_no_leidas(
    current_user: UsuarioReadWithRoles = Depends(get_current_active_user)
):
    """Obtiene el contador de notificaciones no leídas"""
    try:
        codigo_trabajador = obtener_codigo_trabajador(current_user)
        
        resultado = await VacacionesPermisosService.obtener_notificaciones(
            codigo_trabajador=codigo_trabajador,
            page=1,
            limit=1
        )
        
        return {
            'no_leidas': resultado.get('no_leidas', 0),
            'total': resultado.get('total', 0)
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Error contando notificaciones: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error al contar notificaciones"
        )


# ============================================
# ENDPOINTS DE INFORMACIÓN
# ============================================

@router.get(
    "/mi-saldo",
    response_model=SaldoVacacionesRead,
    summary="Obtener mi saldo de vacaciones",
    description="Obtiene el saldo de vacaciones del usuario autenticado"
)
async def obtener_mi_saldo(
    current_user: UsuarioReadWithRoles = Depends(get_current_active_user)
):
    """Obtiene el saldo de vacaciones del usuario"""
    try:
        codigo_trabajador = obtener_codigo_trabajador(current_user)
        
        saldo = await VacacionesPermisosService.obtener_saldo_vacaciones(codigo_trabajador)
        
        if not saldo:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="No se pudo obtener el saldo de vacaciones"
            )
        
        return saldo
        
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Error obteniendo saldo: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error al obtener saldo de vacaciones"
        )


@router.get(
    "/catalogos",
    response_model=CatalogosResponse,
    summary="Obtener catálogos",
    description="Obtiene todos los catálogos necesarios (áreas, secciones, cargos, tipos de permiso)"
)
async def obtener_catalogos(
    current_user: UsuarioReadWithRoles = Depends(get_current_active_user)
):
    """Obtiene los catálogos del sistema"""
    try:
        catalogos = await VacacionesPermisosService.obtener_catalogos()
        
        return CatalogosResponse(
            areas=[CatalogoItem(**item) for item in catalogos['areas']],
            secciones=[CatalogoItem(**item) for item in catalogos['secciones']],
            cargos=[CatalogoItem(**item) for item in catalogos['cargos']],
            tipos_permiso=[
                _map_tipo_permiso_catalogo(item)
                for item in catalogos['tipos_permiso']
            ],
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Error obteniendo catálogos: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error al obtener catálogos"
        )


@router.get(
    "/verificar-aprobador",
    response_model=Dict,
    summary="Verificar si es aprobador",
    description="Verifica si el usuario autenticado está configurado como aprobador en la jerarquía"
)
async def verificar_es_aprobador(
    current_user: UsuarioReadWithRoles = Depends(get_current_active_user)
):
    """Verifica si el usuario es aprobador"""
    try:
        codigo_trabajador = obtener_codigo_trabajador(current_user)
        
        resultado = await VacacionesPermisosService.verificar_es_aprobador(codigo_trabajador)
        
        return resultado
        
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Error verificando si es aprobador: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error al verificar si es aprobador"
        )


# ============================================
# ENDPOINTS DE TRABAJADORES Y CUMPLEAÑOS
# ============================================

@router.get(
    "/cumpleanos-hoy",
    response_model=PaginatedTrabajadorResponse,
    summary="Lista de cumpleaños del día",
    description="Obtiene la lista de trabajadores que cumplen años hoy con paginación"
)
async def obtener_cumpleanos_hoy(
    page: int = Query(1, ge=1, description="Número de página"),
    limit: int = Query(20, ge=1, le=100, description="Elementos por página"),
    current_user: UsuarioReadWithRoles = Depends(get_current_active_user)
):
    """Obtiene la lista de trabajadores que cumplen años hoy con paginación"""
    try:
        resultado = await VacacionesPermisosService.obtener_cumpleanos_hoy(
            page=page,
            limit=limit
        )
        return resultado
        
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Error obteniendo cumpleaños: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error al obtener lista de cumpleaños"
        )


@router.get(
    "/trabajadores",
    response_model=PaginatedTrabajadorResponse,
    summary="Listar trabajadores",
    description="Obtiene la lista completa de trabajadores con paginación y filtros"
)
async def listar_trabajadores(
    codigo: Optional[str] = Query(None, description="Buscar por código de trabajador"),
    nombre: Optional[str] = Query(None, description="Buscar por nombre completo"),
    codigo_area: Optional[str] = Query(None, description="Filtrar por código de área"),
    codigo_seccion: Optional[str] = Query(None, description="Filtrar por código de sección"),
    codigo_cargo: Optional[str] = Query(None, description="Filtrar por código de cargo"),
    page: int = Query(1, ge=1, description="Número de página"),
    limit: int = Query(20, ge=1, le=100, description="Elementos por página"),
    current_user: UsuarioReadWithRoles = Depends(get_current_active_user)
):
    """Lista trabajadores con paginación y filtros"""
    try:
        resultado = await VacacionesPermisosService.listar_trabajadores(
            codigo=codigo,
            nombre=nombre,
            codigo_area=codigo_area,
            codigo_seccion=codigo_seccion,
            codigo_cargo=codigo_cargo,
            page=page,
            limit=limit
        )
        
        return resultado
        
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Error listando trabajadores: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error al listar trabajadores"
        )


# ============================================
# ENDPOINTS DE BOLETAS Y CERTIFICADOS CTS
# ============================================

@router.get(
    "/boleta-pago",
    response_model=BoletasPagoResponse,
    summary="Obtener boleta(s) de pago",
    description="""
    Obtiene boleta(s) de pago del trabajador.
    
    **Comportamiento:**
    - Solo **anio**: devuelve todas las boletas del año (lista).
    - **anio** y **mes**: devuelve solo la boleta de ese año y mes (lista con 1 ítem).
    
    **Parámetros:**
    - anio: Año de la boleta (formato: YYYY, ej: 2025) — requerido
    - mes: Mes de la boleta (formato: MM, ej: 09) — opcional. Si no se envía, se devuelven todas las boletas del año.
    
    **Respuesta:** Siempre un objeto con clave `items` (lista). Cada ítem tiene:
    - codigo_trabajador, anio, mes
    - archivo_pdf_base64: PDF en base64
    - nombre_archivo: Nombre sugerido para guardar
    
    **Uso en Frontend:**
    - Web/Mobile: Recorrer `items` y descargar o visualizar cada PDF.
    """
)
async def obtener_boleta_pago(
    anio: str = Query(..., description="Año de la boleta (YYYY)", regex="^[0-9]{4}$"),
    mes: Optional[str] = Query(None, description="Mes de la boleta (MM). Opcional: si no se envía, se devuelven todas las boletas del año.", regex="^[0-9]{2}$"),
    current_user: UsuarioReadWithRoles = Depends(get_current_active_user)
):
    """
    Obtiene boleta(s) de pago del usuario autenticado.
    Si solo se envía anio, devuelve todas las boletas del año; si se envía anio y mes, solo esa boleta.
    """
    try:
        codigo_trabajador = obtener_codigo_trabajador(current_user)
        
        resultado = await VacacionesPermisosService.obtener_boletas_pago(
            codigo_trabajador=codigo_trabajador,
            anio=anio,
            mes=mes
        )
        
        logger.info(f"Boletas de pago obtenidas para trabajador {codigo_trabajador}, año {anio}, mes={mes} -> {len(resultado.get('items', []))} ítem(s)")
        return resultado
        
    except HTTPException:
        raise
    except CustomException as ce:
        logger.warning(f"Error de negocio al obtener boletas de pago: {ce.detail}")
        raise HTTPException(
            status_code=ce.status_code,
            detail=ce.detail
        )
    except Exception as e:
        logger.exception(f"Error inesperado obteniendo boletas de pago: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error al obtener las boletas de pago"
        )


@router.get(
    "/certificado-cts",
    response_model=CertificadosCTSResponse,
    summary="Obtener certificados CTS",
    description="""
    Obtiene todos los certificados CTS del trabajador para un año.
    Normalmente hay 2 certificados por año (ej. mayo y noviembre).
    Si el año está en curso puede haber 1 registro parcial.
    
    **Parámetros:**
    - anio: Año del certificado (formato: YYYY, ej: 2024)
    
    **Respuesta:** Objeto con clave `items` (lista de certificados). Cada ítem tiene:
    - codigo_trabajador, anio, mes (ej. 05, 11)
    - archivo_pdf_base64: PDF en base64
    - nombre_archivo: Nombre sugerido para guardar
    
    **Uso en Frontend:**
    - Recorrer `items` y descargar o visualizar cada PDF.
    """
)
async def obtener_certificado_cts(
    anio: str = Query(..., description="Año del certificado (YYYY)", regex="^[0-9]{4}$"),
    current_user: UsuarioReadWithRoles = Depends(get_current_active_user)
):
    """
    Obtiene todos los certificados CTS del usuario autenticado para el año (normalmente 2).
    """
    try:
        codigo_trabajador = obtener_codigo_trabajador(current_user)
        
        resultado = await VacacionesPermisosService.obtener_certificados_cts(
            codigo_trabajador=codigo_trabajador,
            anio=anio
        )
        
        logger.info(f"Certificados CTS obtenidos para trabajador {codigo_trabajador}, año {anio} -> {len(resultado.get('items', []))} ítem(s)")
        return resultado
        
    except HTTPException:
        raise
    except CustomException as ce:
        logger.warning(f"Error de negocio al obtener certificados CTS: {ce.detail}")
        raise HTTPException(
            status_code=ce.status_code,
            detail=ce.detail
        )
    except Exception as e:
        logger.exception(f"Error inesperado obteniendo certificados CTS: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error al obtener los certificados CTS"
        )


@router.get(
    "/documento-pago",
    response_model=BoletasPagoResponse,
    summary="Obtener documento(s) de pago",
    description="""
    Obtiene documento(s) de pago del trabajador (ctpdoc = 'O').
    
    **Comportamiento:**
    - Solo **anio**: devuelve todos los documentos de pago del año (lista).
    - **anio** y **mes**: devuelve solo los documentos de ese año y mes.
    """
)
async def obtener_documento_pago(
    anio: str = Query(..., description="Año del documento de pago (YYYY)", regex="^[0-9]{4}$"),
    mes: Optional[str] = Query(None, description="Mes del documento (MM). Opcional", regex="^[0-9]{2}$"),
    current_user: UsuarioReadWithRoles = Depends(get_current_active_user)
):
    try:
        codigo_trabajador = obtener_codigo_trabajador(current_user)
        resultado = await VacacionesPermisosService.obtener_documentos_pago(
            codigo_trabajador=codigo_trabajador,
            anio=anio,
            mes=mes
        )
        logger.info(
            f"Documentos de pago obtenidos para trabajador {codigo_trabajador}, año {anio}, mes={mes} "
            f"-> {len(resultado.get('items', []))} ítem(s)"
        )
        return resultado
    except HTTPException:
        raise
    except CustomException as ce:
        logger.warning(f"Error de negocio al obtener documentos de pago: {ce.detail}")
        raise HTTPException(status_code=ce.status_code, detail=ce.detail)
    except Exception as e:
        logger.exception(f"Error inesperado obteniendo documentos de pago: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error al obtener los documentos de pago"
        )


@router.get(
    "/documentos-empresa",
    response_model=DocumentosEmpresaResponse,
    summary="Obtener documentos de empresa",
    description="Obtiene documentos de empresa (ctpdoc = 'E') para mostrar en la app."
)
async def obtener_documentos_empresa(
    current_user: UsuarioReadWithRoles = Depends(get_current_active_user)
):
    try:
        resultado = await VacacionesPermisosService.obtener_documentos_empresa()
        logger.info(f"Documentos de empresa obtenidos -> {len(resultado.get('items', []))} ítem(s)")
        return resultado
    except HTTPException:
        raise
    except CustomException as ce:
        logger.warning(f"Error de negocio al obtener documentos de empresa: {ce.detail}")
        raise HTTPException(status_code=ce.status_code, detail=ce.detail)
    except Exception as e:
        logger.exception(f"Error inesperado obteniendo documentos de empresa: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error al obtener los documentos de empresa"
        )


@router.get(
    "/avisos-empresa",
    response_model=AvisosEmpresaResponse,
    summary="Obtener avisos de empresa",
    description="Obtiene avisos de empresa (ctpdoc = 'A') para mostrar en la app."
)
async def obtener_avisos_empresa(
    current_user: UsuarioReadWithRoles = Depends(get_current_active_user)
):
    try:
        resultado = await VacacionesPermisosService.obtener_avisos_empresa()
        logger.info(f"Avisos de empresa obtenidos -> {len(resultado.get('items', []))} ítem(s)")
        return resultado
    except HTTPException:
        raise
    except CustomException as ce:
        logger.warning(f"Error de negocio al obtener avisos de empresa: {ce.detail}")
        raise HTTPException(status_code=ce.status_code, detail=ce.detail)
    except Exception as e:
        logger.exception(f"Error inesperado obteniendo avisos de empresa: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error al obtener los avisos de empresa"
        )

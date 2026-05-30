# app/services/vacaciones_permisos_service.py
"""
Servicio para gestión completa del Sistema de Vacaciones y Permisos.

Este servicio maneja toda la lógica de negocio relacionada con:
- Creación y gestión de solicitudes de vacaciones y permisos
- Flujo de aprobación multi-nivel
- Notificaciones y comunicación
- Cálculo de saldos y validaciones
- Configuración de flujos y jerarquías
"""

from datetime import datetime, date, time, timedelta
from decimal import Decimal
from typing import Dict, List, Optional, Any, Tuple
import logging
import math

# 🗄️ IMPORTACIONES DE BASE DE DATOS
from app.db.queries import (
    execute_query, execute_insert, execute_update, execute_transaction,
    # Solicitudes
    INSERT_SOLICITUD, SELECT_SOLICITUD_BY_ID, SELECT_SOLICITUDES_BY_TRABAJADOR,
    SELECT_SOLICITUDES_PAGINATED, COUNT_SOLICITUDES, COUNT_SOLICITUDES_SOLAPADAS,
    UPDATE_SOLICITUD, ANULAR_SOLICITUD,
    # Aprobaciones
    INSERT_APROBACION, SELECT_APROBACIONES_BY_SOLICITUD, SELECT_APROBACIONES_PENDIENTES,
    UPDATE_APROBACION, UPDATE_SOLICITUD_ESTADO,
    # Notificaciones
    INSERT_NOTIFICACION, SELECT_NOTIFICACIONES_BY_TRABAJADOR,
    SELECT_NOTIFICACIONES_PAGINATED, COUNT_NOTIFICACIONES, UPDATE_NOTIFICACION_LEIDA,
    # Configuración
    SELECT_CONFIG_FLUJO_APLICABLE, SELECT_JERARQUIA_APROBADORES, SELECT_SUSTITUTO_ACTIVO,
    SELECT_ALL_CONFIG_FLUJO, SELECT_ALL_JERARQUIA, SELECT_ALL_SUSTITUTOS,
    INSERT_CONFIG_FLUJO, INSERT_JERARQUIA, INSERT_SUSTITUTO,
    UPDATE_CONFIG_FLUJO, SELECT_CONFIG_FLUJO_BY_ID, DELETE_CONFIG_FLUJO,
    UPDATE_JERARQUIA, SELECT_JERARQUIA_BY_ID, DELETE_JERARQUIA,
    SELECT_ES_APROBADOR, SELECT_NIVELES_APROBADOR,
    # Saldos y catálogos
    SELECT_SALDO_VACACIONES, SELECT_TRABAJADOR_BY_CODIGO,
    SELECT_CATALOGO_AREAS, SELECT_CATALOGO_SECCIONES, SELECT_CATALOGO_CARGOS,
    SELECT_CATALOGO_TIPOS_PERMISO, SELECT_TIPO_PERMISO_BY_CODIGO,
    SELECT_CUMPLEANOS_HOY, COUNT_CUMPLEANOS_HOY, SELECT_TRABAJADORES_PAGINATED, COUNT_TRABAJADORES,
    # Estadísticas
    SELECT_ESTADISTICAS_SOLICITUDES, SELECT_SOLICITUDES_POR_MES, SELECT_ALL_SALDOS_VACACIONES
)

# 📋 SCHEMAS
from app.schemas.vacaciones_permisos import (
    SolicitudCreate, SolicitudUpdate, SolicitudRead, SolicitudReadFull,
    AprobacionCreate, AprobacionRead, AprobacionUpdate,
    NotificacionRead, SaldoVacacionesRead, CatalogoItem, CatalogosResponse,
    TrabajadorRead, PaginatedTrabajadorResponse
)

# 🚨 EXCEPCIONES
from app.core.exceptions import (
    ValidationError, NotFoundError, ConflictError, ServiceError, DatabaseError
)

# 🏗️ BASE SERVICE
from app.services.base_service import BaseService

logger = logging.getLogger(__name__)


class VacacionesPermisosService(BaseService):
    """
    Servicio principal para gestión de vacaciones y permisos.
    
    CARACTERÍSTICAS:
    - Herencia de BaseService para manejo automático de errores
    - Validaciones robustas de negocio
    - Manejo de flujo de aprobación multi-nivel
    - Integración con sistema de notificaciones
    """

    # ============================================
    # MÉTODOS DE CÁLCULO Y VALIDACIÓN
    # ============================================

    @staticmethod
    def calcular_dias_solicitados(fecha_inicio: date, fecha_fin: date) -> Decimal:
        """
        Calcula los días solicitados entre dos fechas (incluyendo ambos días).
        
        Args:
            fecha_inicio: Fecha de inicio
            fecha_fin: Fecha de fin
            
        Returns:
            Decimal: Número de días solicitados
        """
        if fecha_fin < fecha_inicio:
            raise ValidationError(detail="fecha_fin debe ser mayor o igual a fecha_inicio")
        
        dias = (fecha_fin - fecha_inicio).days + 1
        return Decimal(str(dias))

    @staticmethod
    def calcular_horas_solicitadas(hora_inicio: time, hora_fin: time) -> Decimal:
        """Calcula horas decimales entre dos horas del mismo día."""
        inicio_min = hora_inicio.hour * 60 + hora_inicio.minute + hora_inicio.second / 60
        fin_min = hora_fin.hour * 60 + hora_fin.minute + hora_fin.second / 60
        if fin_min <= inicio_min:
            raise ValidationError(
                detail="hora_fin debe ser posterior a hora_inicio",
                internal_code="PERMISO_HORA_RANGO_INVALIDO",
            )
        horas = round((fin_min - inicio_min) / 60, 2)
        return Decimal(str(horas))

    @staticmethod
    def resolver_modo_tiempo(tipo_solicitud: str, codigo_permiso: Optional[str]) -> str:
        """
        Resuelve D (día) o H (hora) según tipo de solicitud y catálogo de permisos.
        Vacaciones siempre D. Registros históricos sin hora se tratan como D.
        """
        if tipo_solicitud == 'V':
            return 'D'
        if not codigo_permiso:
            raise ValidationError(
                detail="codigo_permiso es obligatorio para permisos",
                internal_code="PERMISO_CODIGO_REQUERIDO",
            )
        row = execute_query(SELECT_TIPO_PERMISO_BY_CODIGO, (codigo_permiso,))
        if not row:
            raise ValidationError(
                detail=f"Tipo de permiso '{codigo_permiso}' no encontrado en el catálogo",
                internal_code="PERMISO_CATALOGO_INVALIDO",
            )
        tiempo = (row[0].get('tiempo') or '').strip().upper()
        if tiempo not in ('D', 'H'):
            raise ValidationError(
                detail=f"Tipo de permiso '{codigo_permiso}' sin ctiempo válido (D/H)",
                internal_code="PERMISO_CATALOGO_INVALIDO",
            )
        return tiempo

    @staticmethod
    def _resolver_tiempo_solicitud(solicitud: Dict[str, Any]) -> str:
        """Inferencia de modo tiempo para lectura (compatibilidad histórica)."""
        if solicitud.get('tipo_solicitud') == 'V':
            return 'D'
        if solicitud.get('hora_inicio') is not None:
            return 'H'
        tc = solicitud.get('tiempo_catalogo') or solicitud.get('tiempo')
        if isinstance(tc, str) and tc.strip().upper() in ('D', 'H'):
            return tc.strip().upper()
        return 'D'

    @staticmethod
    def _enriquecer_solicitud(solicitud: Dict[str, Any]) -> Dict[str, Any]:
        solicitud['tiempo'] = VacacionesPermisosService._resolver_tiempo_solicitud(solicitud)
        return solicitud

    @staticmethod
    def validar_campos_segun_modo(
        modo: str,
        tipo_solicitud: str,
        fecha_inicio: date,
        fecha_fin: date,
        hora_inicio: Optional[time],
        hora_fin: Optional[time],
    ) -> None:
        if modo == 'H':
            if fecha_inicio != fecha_fin:
                raise ValidationError(
                    detail="Permiso por horas: fecha_inicio y fecha_fin deben ser el mismo día",
                    internal_code="PERMISO_HORA_MISMO_DIA",
                )
            if hora_inicio is None or hora_fin is None:
                raise ValidationError(
                    detail="Permiso por horas: hora_inicio y hora_fin son obligatorias",
                    internal_code="PERMISO_HORA_REQUERIDAS",
                )
            if hora_fin <= hora_inicio:
                raise ValidationError(
                    detail="hora_fin debe ser posterior a hora_inicio",
                    internal_code="PERMISO_HORA_RANGO_INVALIDO",
                )
        else:
            if hora_inicio is not None or hora_fin is not None:
                raise ValidationError(
                    detail="Permiso por días o vacaciones: no envíe hora_inicio ni hora_fin",
                    internal_code="PERMISO_DIA_SIN_HORAS",
                )
        if tipo_solicitud == 'V' and (hora_inicio is not None or hora_fin is not None):
            raise ValidationError(
                detail="Las solicitudes de vacaciones no admiten horas",
                internal_code="VACACIONES_SIN_HORAS",
            )

    @staticmethod
    @BaseService.handle_service_errors
    async def validar_saldo_suficiente(
        codigo_trabajador: str,
        dias_solicitados: Decimal,
        tipo_solicitud: str
    ) -> bool:
        """
        Valida que el trabajador tenga saldo suficiente para la solicitud.
        
        Args:
            codigo_trabajador: Código del trabajador
            dias_solicitados: Días que se solicitan
            tipo_solicitud: 'V' para vacaciones, 'P' para permisos
            
        Returns:
            bool: True si hay saldo suficiente
            
        Raises:
            ValidationError: Si no hay saldo suficiente
        """
        if tipo_solicitud != 'V':
            # Los permisos no requieren validación de saldo
            return True
        
        try:
            saldo = await VacacionesPermisosService.obtener_saldo_vacaciones(codigo_trabajador)
            if not saldo:
                raise ValidationError(detail="No se pudo obtener el saldo de vacaciones")
            
            saldo_disponible = Decimal(str(saldo.get('saldo_disponible', 0)))
            
            if saldo_disponible < dias_solicitados:
                raise ValidationError(
                    detail=f"Saldo insuficiente. Disponible: {saldo_disponible}, Solicitado: {dias_solicitados}"
                )
            
            return True
            
        except ValidationError:
            raise
        except Exception as e:
            logger.error(f"Error validando saldo: {str(e)}")
            raise ServiceError(
                status_code=500,
                detail="Error al validar saldo de vacaciones",
                internal_code="SALDO_VALIDATION_ERROR"
            )

    @staticmethod
    @BaseService.handle_service_errors
    async def validar_sin_solapamiento(
        codigo_trabajador: str,
        fecha_inicio: date,
        fecha_fin: date,
        modo: str,
        hora_inicio: Optional[time] = None,
        hora_fin: Optional[time] = None,
        id_solicitud_excluir: Optional[int] = None,
    ) -> None:
        """
        Valida solapamiento con solicitudes activas (P o A).
        Rama día: cruce de fechas si al menos uno es por día.
        Rama hora: mismo criterio de fechas más cruce de horas si ambos son por hora.
        """
        nueva_es_hora = 1 if modo == 'H' else 0
        resultado = execute_query(
            COUNT_SOLICITUDES_SOLAPADAS,
            (
                codigo_trabajador,
                id_solicitud_excluir,
                id_solicitud_excluir,
                nueva_es_hora,
                fecha_fin,
                fecha_inicio,
                nueva_es_hora,
                fecha_fin,
                fecha_inicio,
                hora_fin if modo == 'H' else None,
                hora_inicio if modo == 'H' else None,
            ),
        )
        total = int(resultado[0]['total']) if resultado else 0
        if total > 0:
            detail = (
                "Ya existe una solicitud pendiente o aprobada que se cruza "
                "con el rango de horas indicado."
                if modo == 'H'
                else (
                    "Ya existe una solicitud pendiente o aprobada que se cruza "
                    "con el rango de fechas indicado."
                )
            )
            code = (
                "SOLICITUD_HORAS_SOLAPADAS"
                if modo == 'H'
                else "SOLICITUD_FECHAS_SOLAPADAS"
            )
            raise ValidationError(detail=detail, internal_code=code)

    @staticmethod
    @BaseService.handle_service_errors
    async def validar_sin_solapamiento_fechas(
        codigo_trabajador: str,
        fecha_inicio: date,
        fecha_fin: date,
        id_solicitud_excluir: Optional[int] = None,
    ) -> None:
        """Compatibilidad: asume modo día."""
        await VacacionesPermisosService.validar_sin_solapamiento(
            codigo_trabajador=codigo_trabajador,
            fecha_inicio=fecha_inicio,
            fecha_fin=fecha_fin,
            modo='D',
            id_solicitud_excluir=id_solicitud_excluir,
        )

    # ============================================
    # MÉTODOS DE SOLICITUDES
    # ============================================

    @staticmethod
    @BaseService.handle_service_errors
    async def crear_solicitud(
        solicitud_data: SolicitudCreate,
        usuario_registro: str
    ) -> Dict[str, Any]:
        """
        Crea una nueva solicitud de vacaciones o permiso.
        
        Proceso:
        1. Validar datos
        2. Calcular días solicitados
        3. Validar saldo (si es vacación)
        4. Insertar solicitud
        5. Determinar flujo de aprobación
        6. Crear registros de aprobación
        7. Crear notificaciones
        
        Args:
            solicitud_data: Datos de la solicitud
            usuario_registro: Usuario que crea la solicitud
            
        Returns:
            Dict: Datos de la solicitud creada
        """
        try:
            modo = VacacionesPermisosService.resolver_modo_tiempo(
                solicitud_data.tipo_solicitud,
                solicitud_data.codigo_permiso,
            )
            VacacionesPermisosService.validar_campos_segun_modo(
                modo=modo,
                tipo_solicitud=solicitud_data.tipo_solicitud,
                fecha_inicio=solicitud_data.fecha_inicio,
                fecha_fin=solicitud_data.fecha_fin,
                hora_inicio=solicitud_data.hora_inicio,
                hora_fin=solicitud_data.hora_fin,
            )

            hora_inicio_db: Optional[time] = None
            hora_fin_db: Optional[time] = None
            horas_solicitadas: Optional[Decimal] = None

            if modo == 'H':
                dias_solicitados = Decimal('0')
                hora_inicio_db = solicitud_data.hora_inicio
                hora_fin_db = solicitud_data.hora_fin
                horas_solicitadas = VacacionesPermisosService.calcular_horas_solicitadas(
                    hora_inicio_db, hora_fin_db
                )
            else:
                dias_solicitados = VacacionesPermisosService.calcular_dias_solicitados(
                    solicitud_data.fecha_inicio,
                    solicitud_data.fecha_fin,
                )

            await VacacionesPermisosService.validar_sin_solapamiento(
                codigo_trabajador=solicitud_data.codigo_trabajador,
                fecha_inicio=solicitud_data.fecha_inicio,
                fecha_fin=solicitud_data.fecha_fin,
                modo=modo,
                hora_inicio=hora_inicio_db,
                hora_fin=hora_fin_db,
            )

            if solicitud_data.tipo_solicitud == 'V':
                await VacacionesPermisosService.validar_saldo_suficiente(
                    solicitud_data.codigo_trabajador,
                    dias_solicitados,
                    solicitud_data.tipo_solicitud,
                )

            params = (
                solicitud_data.tipo_solicitud,
                solicitud_data.codigo_permiso,
                solicitud_data.codigo_trabajador,
                solicitud_data.fecha_inicio,
                solicitud_data.fecha_fin,
                hora_inicio_db,
                hora_fin_db,
                dias_solicitados,
                horas_solicitadas,
                solicitud_data.observacion,
                solicitud_data.motivo,
                usuario_registro,
            )

            resultado = execute_insert(INSERT_SOLICITUD, params)
            
            if not resultado or 'id_solicitud' not in resultado:
                raise ServiceError(
                    status_code=500,
                    detail="Error al crear la solicitud",
                    internal_code="SOLICITUD_CREATE_ERROR"
                )
            
            id_solicitud = resultado['id_solicitud']
            logger.info(f"Solicitud {id_solicitud} creada exitosamente")
            
            # 4. Determinar y crear flujo de aprobación
            try:
                await VacacionesPermisosService._crear_flujo_aprobacion(
                    id_solicitud=id_solicitud,
                    tipo_solicitud=solicitud_data.tipo_solicitud,
                    codigo_permiso=solicitud_data.codigo_permiso,
                    codigo_trabajador=solicitud_data.codigo_trabajador,
                    dias_solicitados=dias_solicitados,
                    modo=modo,
                )
            except ServiceError as e:
                # Si falla la creación del flujo, eliminar la solicitud creada
                if e.internal_code == "JERARQUIA_INCOMPLETA":
                    try:
                        from app.db.queries import execute_update
                        execute_update(
                            "UPDATE ppavac_solicitud SET estado = 'N', fecha_anulacion = GETDATE(), motivo_anulacion = ? WHERE id_solicitud = ?",
                            (f"Anulada automáticamente: {e.detail}", id_solicitud)
                        )
                        logger.warning(f"Solicitud {id_solicitud} anulada automáticamente debido a jerarquía incompleta")
                    except Exception as cleanup_error:
                        logger.error(f"Error al anular solicitud {id_solicitud}: {str(cleanup_error)}")
                raise
            
            # 5. Enviar notificación push a aprobadores (en background, no bloquea)
            try:
                logger.info(f"Iniciando envío de notificación push para solicitud {id_solicitud}")
                
                # Obtener información del trabajador para la notificación
                trabajador = execute_query(
                    SELECT_TRABAJADOR_BY_CODIGO,
                    (solicitud_data.codigo_trabajador,)
                )
                nombre_trabajador = trabajador[0].get('nombre_completo', 'Trabajador') if trabajador else 'Trabajador'
                
                # Obtener código de área del trabajador
                codigo_area = None
                if trabajador and trabajador[0].get('codigo_area'):
                    codigo_area = trabajador[0]['codigo_area']
                    logger.info(f"Área del trabajador {solicitud_data.codigo_trabajador}: {codigo_area}")
                else:
                    logger.warning(f"No se pudo obtener área para trabajador {solicitud_data.codigo_trabajador}")
                
                # Enviar notificación
                from app.services.notificaciones_service import NotificacionesService
                resultado_notif = await NotificacionesService.enviar_notificacion_nueva_solicitud(
                    id_solicitud=id_solicitud,
                    tipo_solicitud=solicitud_data.tipo_solicitud,
                    codigo_trabajador=solicitud_data.codigo_trabajador,
                    nombre_trabajador=nombre_trabajador,
                    codigo_area=codigo_area
                )
                
                if resultado_notif.get('enviado'):
                    logger.info(
                        f"✅ Notificación push enviada exitosamente para solicitud {id_solicitud}: "
                        f"{resultado_notif.get('success_count', 0)} dispositivos notificados"
                    )
                else:
                    logger.warning(
                        f"⚠️ No se pudo enviar notificación push para solicitud {id_solicitud}: "
                        f"{resultado_notif.get('mensaje', 'Error desconocido')}"
                    )
                    
            except Exception as notif_error:
                # No fallar la creación de solicitud si falla la notificación
                logger.exception(f"❌ Error crítico enviando notificación push (no crítico para creación): {str(notif_error)}")
            
            # 6. Obtener solicitud completa
            solicitud = await VacacionesPermisosService.obtener_solicitud(id_solicitud)
            
            BaseService.log_operation_success("Creación de solicitud", id_solicitud)
            return solicitud
            
        except (ValidationError, ConflictError, ServiceError):
            raise
        except Exception as e:
            logger.exception(f"Error inesperado creando solicitud: {str(e)}")
            raise ServiceError(
                status_code=500,
                detail="Error interno al crear solicitud",
                internal_code="SOLICITUD_CREATE_UNEXPECTED_ERROR"
            )

    @staticmethod
    @BaseService.handle_service_errors
    async def obtener_solicitud(id_solicitud: int) -> Dict[str, Any]:
        """
        Obtiene una solicitud por su ID con todos sus detalles.
        
        Args:
            id_solicitud: ID de la solicitud
            
        Returns:
            Dict: Datos completos de la solicitud con aprobaciones
            
        Raises:
            NotFoundError: Si la solicitud no existe
        """
        try:
            resultado = execute_query(SELECT_SOLICITUD_BY_ID, (id_solicitud,))
            
            if not resultado:
                raise NotFoundError(
                    detail=f"Solicitud con ID {id_solicitud} no encontrada",
                    internal_code="SOLICITUD_NOT_FOUND"
                )
            
            solicitud = resultado[0]
            
            # Obtener aprobaciones
            aprobaciones = await VacacionesPermisosService.obtener_aprobaciones(id_solicitud)
            solicitud['aprobaciones'] = aprobaciones
            
            # Obtener información adicional del trabajador
            trabajador = execute_query(
                SELECT_TRABAJADOR_BY_CODIGO,
                (solicitud['codigo_trabajador'],)
            )
            if trabajador:
                solicitud['nombre_trabajador'] = trabajador[0].get('nombre_completo')

            return VacacionesPermisosService._enriquecer_solicitud(solicitud)

        except NotFoundError:
            raise
        except Exception as e:
            logger.exception(f"Error obteniendo solicitud {id_solicitud}: {str(e)}")
            raise ServiceError(
                status_code=500,
                detail="Error al obtener solicitud",
                internal_code="SOLICITUD_GET_ERROR"
            )

    @staticmethod
    @BaseService.handle_service_errors
    async def listar_solicitudes(
        codigo_trabajador: Optional[str] = None,
        estado: Optional[str] = None,
        tipo_solicitud: Optional[str] = None,
        fecha_desde: Optional[date] = None,
        fecha_hasta: Optional[date] = None,
        page: int = 1,
        limit: int = 20
    ) -> Dict[str, Any]:
        """
        Lista solicitudes con paginación y filtros.
        
        Args:
            codigo_trabajador: Filtrar por trabajador
            estado: Filtrar por estado (P, A, R, N)
            tipo_solicitud: Filtrar por tipo (V, P)
            fecha_desde: Fecha inicio del rango
            fecha_hasta: Fecha fin del rango
            page: Número de página
            limit: Elementos por página
            
        Returns:
            Dict: Resultado paginado con items y metadatos
        """
        try:
            # Validar parámetros
            if page < 1:
                page = 1
            if limit < 1 or limit > 100:
                limit = 20
            
            offset = (page - 1) * limit
            limit_end = offset + limit
            
            # Parámetros para la query
            params = (
                codigo_trabajador, codigo_trabajador,  # Para el filtro
                estado, estado,
                tipo_solicitud, tipo_solicitud,
                fecha_desde, fecha_desde, fecha_hasta, fecha_hasta,
                offset + 1, limit_end  # Rango ROW_NUMBER
            )
            
            # Obtener items
            items = execute_query(SELECT_SOLICITUDES_PAGINATED, params)
            
            # Obtener total
            count_params = (
                codigo_trabajador, codigo_trabajador,
                estado, estado,
                tipo_solicitud, tipo_solicitud,
                fecha_desde, fecha_desde, fecha_hasta, fecha_hasta
            )
            total_result = execute_query(COUNT_SOLICITUDES, count_params)
            total = total_result[0]['total'] if total_result else 0
            
            pages = math.ceil(total / limit) if total > 0 else 0

            items_enriquecidos = [
                VacacionesPermisosService._enriquecer_solicitud(dict(item))
                for item in (items or [])
            ]

            return {
                'items': items_enriquecidos,
                'total': total,
                'page': page,
                'limit': limit,
                'pages': pages
            }
            
        except Exception as e:
            logger.exception(f"Error listando solicitudes: {str(e)}")
            raise ServiceError(
                status_code=500,
                detail="Error al listar solicitudes",
                internal_code="SOLICITUD_LIST_ERROR"
            )

    @staticmethod
    @BaseService.handle_service_errors
    async def actualizar_solicitud(
        id_solicitud: int,
        solicitud_data: SolicitudUpdate,
        usuario_modificacion: str
    ) -> Dict[str, Any]:
        """
        Actualiza una solicitud (solo si está pendiente).
        
        Args:
            id_solicitud: ID de la solicitud
            solicitud_data: Datos a actualizar
            usuario_modificacion: Usuario que modifica
            
        Returns:
            Dict: Datos actualizados de la solicitud
            
        Raises:
            NotFoundError: Si la solicitud no existe
            ValidationError: Si la solicitud no está pendiente
        """
        try:
            # Verificar que existe y está pendiente
            solicitud_actual = await VacacionesPermisosService.obtener_solicitud(id_solicitud)
            
            if solicitud_actual['estado'] != 'P':
                raise ValidationError(
                    detail="Solo se pueden actualizar solicitudes pendientes",
                    internal_code="SOLICITUD_NOT_PENDING"
                )
            
            fecha_inicio = solicitud_data.fecha_inicio or solicitud_actual['fecha_inicio']
            fecha_fin = solicitud_data.fecha_fin or solicitud_actual['fecha_fin']
            hora_inicio = (
                solicitud_data.hora_inicio
                if solicitud_data.hora_inicio is not None
                else solicitud_actual.get('hora_inicio')
            )
            hora_fin = (
                solicitud_data.hora_fin
                if solicitud_data.hora_fin is not None
                else solicitud_actual.get('hora_fin')
            )

            modo = VacacionesPermisosService.resolver_modo_tiempo(
                solicitud_actual['tipo_solicitud'],
                solicitud_actual.get('codigo_permiso'),
            )
            VacacionesPermisosService.validar_campos_segun_modo(
                modo=modo,
                tipo_solicitud=solicitud_actual['tipo_solicitud'],
                fecha_inicio=fecha_inicio,
                fecha_fin=fecha_fin,
                hora_inicio=hora_inicio,
                hora_fin=hora_fin,
            )

            cambian_periodo = any([
                solicitud_data.fecha_inicio,
                solicitud_data.fecha_fin,
                solicitud_data.hora_inicio is not None,
                solicitud_data.hora_fin is not None,
            ])

            dias_solicitados = None
            horas_solicitadas = None
            hora_inicio_db = None
            hora_fin_db = None

            if cambian_periodo or modo == 'H':
                if modo == 'H':
                    dias_solicitados = Decimal('0')
                    hora_inicio_db = hora_inicio
                    hora_fin_db = hora_fin
                    horas_solicitadas = VacacionesPermisosService.calcular_horas_solicitadas(
                        hora_inicio_db, hora_fin_db
                    )
                elif solicitud_data.fecha_inicio or solicitud_data.fecha_fin:
                    dias_solicitados = VacacionesPermisosService.calcular_dias_solicitados(
                        fecha_inicio, fecha_fin
                    )
                    hora_inicio_db = None
                    hora_fin_db = None
                    horas_solicitadas = None

            await VacacionesPermisosService.validar_sin_solapamiento(
                codigo_trabajador=solicitud_actual['codigo_trabajador'],
                fecha_inicio=fecha_inicio,
                fecha_fin=fecha_fin,
                modo=modo,
                hora_inicio=hora_inicio if modo == 'H' else None,
                hora_fin=hora_fin if modo == 'H' else None,
                id_solicitud_excluir=id_solicitud,
            )

            if dias_solicitados is not None and solicitud_actual['tipo_solicitud'] == 'V':
                await VacacionesPermisosService.validar_saldo_suficiente(
                    solicitud_actual['codigo_trabajador'],
                    dias_solicitados,
                    solicitud_actual['tipo_solicitud'],
                )

            params = (
                solicitud_data.fecha_inicio,
                solicitud_data.fecha_fin,
                hora_inicio_db if cambian_periodo else solicitud_data.hora_inicio,
                hora_fin_db if cambian_periodo else solicitud_data.hora_fin,
                dias_solicitados,
                horas_solicitadas,
                solicitud_data.observacion,
                solicitud_data.motivo,
                usuario_modificacion,
                id_solicitud,
            )
            
            resultado = execute_update(UPDATE_SOLICITUD, params)
            
            if not resultado or resultado.get('rows_affected', 0) == 0:
                raise NotFoundError(
                    detail=f"Solicitud {id_solicitud} no encontrada o no está pendiente",
                    internal_code="SOLICITUD_UPDATE_ERROR"
                )
            
            BaseService.log_operation_success("Actualización de solicitud", id_solicitud)
            return await VacacionesPermisosService.obtener_solicitud(id_solicitud)
            
        except (NotFoundError, ValidationError):
            raise
        except Exception as e:
            logger.exception(f"Error actualizando solicitud {id_solicitud}: {str(e)}")
            raise ServiceError(
                status_code=500,
                detail="Error al actualizar solicitud",
                internal_code="SOLICITUD_UPDATE_UNEXPECTED_ERROR"
            )

    @staticmethod
    @BaseService.handle_service_errors
    async def anular_solicitud(
        id_solicitud: int,
        motivo_anulacion: str,
        usuario_anulacion: str
    ) -> Dict[str, Any]:
        """
        Anula una solicitud pendiente.
        
        Args:
            id_solicitud: ID de la solicitud
            motivo_anulacion: Motivo de la anulación
            usuario_anulacion: Usuario que anula
            
        Returns:
            Dict: Datos de la solicitud anulada
        """
        try:
            # Verificar que existe y está pendiente
            solicitud = await VacacionesPermisosService.obtener_solicitud(id_solicitud)
            
            if solicitud['estado'] != 'P':
                raise ValidationError(
                    detail="Solo se pueden anular solicitudes pendientes",
                    internal_code="SOLICITUD_NOT_PENDING"
                )
            
            # Anular
            params = (usuario_anulacion, motivo_anulacion, id_solicitud)
            resultado = execute_update(ANULAR_SOLICITUD, params)
            
            if not resultado or resultado.get('rows_affected', 0) == 0:
                raise NotFoundError(
                    detail=f"Solicitud {id_solicitud} no encontrada o no está pendiente",
                    internal_code="SOLICITUD_ANULAR_ERROR"
                )
            
            BaseService.log_operation_success("Anulación de solicitud", id_solicitud)
            return await VacacionesPermisosService.obtener_solicitud(id_solicitud)
            
        except (NotFoundError, ValidationError):
            raise
        except Exception as e:
            logger.exception(f"Error anulando solicitud {id_solicitud}: {str(e)}")
            raise ServiceError(
                status_code=500,
                detail="Error al anular solicitud",
                internal_code="SOLICITUD_ANULAR_UNEXPECTED_ERROR"
            )

    # ============================================
    # MÉTODOS DE APROBACIÓN
    # ============================================

    @staticmethod
    @BaseService.handle_service_errors
    async def obtener_aprobaciones(id_solicitud: int) -> List[Dict[str, Any]]:
        """Obtiene todas las aprobaciones de una solicitud"""
        try:
            return execute_query(SELECT_APROBACIONES_BY_SOLICITUD, (id_solicitud,))
        except Exception as e:
            logger.error(f"Error obteniendo aprobaciones: {str(e)}")
            return []

    @staticmethod
    @BaseService.handle_service_errors
    async def verificar_es_aprobador(
        codigo_trabajador: str
    ) -> Dict[str, Any]:
        """
        Verifica si un trabajador está configurado como aprobador en la jerarquía.
        
        Args:
            codigo_trabajador: Código del trabajador
            
        Returns:
            Dict: Información sobre si es aprobador y en qué niveles
        """
        try:
            resultado = execute_query(
                SELECT_ES_APROBADOR,
                (codigo_trabajador,)
            )
            
            if not resultado or resultado[0]['total_configuraciones'] == 0:
                return {
                    'es_aprobador': False,
                    'total_configuraciones': 0,
                    'niveles': []
                }
            
            info = resultado[0]
            
            # Obtener los niveles específicos
            niveles_result = execute_query(
                SELECT_NIVELES_APROBADOR,
                (codigo_trabajador,)
            )
            niveles = [int(n['nivel_jerarquico']) for n in niveles_result] if niveles_result else []
            
            return {
                'es_aprobador': True,
                'total_configuraciones': info['total_configuraciones'],
                'nivel_minimo': info.get('nivel_minimo'),
                'nivel_maximo': info.get('nivel_maximo'),
                'niveles': sorted(niveles) if niveles else []
            }
        except Exception as e:
            logger.exception(f"Error verificando si es aprobador: {str(e)}")
            raise ServiceError(
                status_code=500,
                detail="Error al verificar si es aprobador",
                internal_code="VERIFICAR_APROBADOR_ERROR"
            )

    @staticmethod
    @BaseService.handle_service_errors
    async def obtener_solicitudes_pendientes_aprobacion(
        codigo_trabajador_aprobador: str
    ) -> List[Dict[str, Any]]:
        """
        Obtiene las solicitudes pendientes de aprobación para un aprobador.
        
        Args:
            codigo_trabajador_aprobador: Código del trabajador aprobador
            
        Returns:
            List: Lista de solicitudes pendientes
        """
        try:
            return execute_query(
                SELECT_APROBACIONES_PENDIENTES,
                (codigo_trabajador_aprobador,)
            )
        except Exception as e:
            logger.exception(f"Error obteniendo solicitudes pendientes: {str(e)}")
            raise ServiceError(
                status_code=500,
                detail="Error al obtener solicitudes pendientes",
                internal_code="APROBACIONES_PENDIENTES_ERROR"
            )

    @staticmethod
    @BaseService.handle_service_errors
    async def aprobar_solicitud(
        id_solicitud: int,
        id_aprobacion: int,
        aprobacion_data: AprobacionUpdate,
        codigo_trabajador_aprobador: str
    ) -> Dict[str, Any]:
        """
        Aprueba una solicitud en un nivel específico.
        
        Proceso:
        1. Validar que el aprobador tiene permiso
        2. Actualizar registro de aprobación
        3. Verificar si hay más niveles
        4. Si es último nivel, marcar solicitud como aprobada
        5. Si hay más niveles, notificar siguiente aprobador
        6. Notificar al solicitante
        
        Args:
            id_solicitud: ID de la solicitud
            id_aprobacion: ID del registro de aprobación
            aprobacion_data: Datos de la aprobación
            codigo_trabajador_aprobador: Código del aprobador
            
        Returns:
            Dict: Resultado de la aprobación
        """
        try:
            # 1. Verificar que la aprobación existe y corresponde al aprobador
            aprobaciones = await VacacionesPermisosService.obtener_aprobaciones(id_solicitud)
            aprobacion_actual = next(
                (a for a in aprobaciones if a['id_aprobacion'] == id_aprobacion),
                None
            )
            
            if not aprobacion_actual:
                raise NotFoundError(
                    detail="Aprobación no encontrada",
                    internal_code="APROBACION_NOT_FOUND"
                )
            
            if aprobacion_actual['codigo_trabajador_aprueba'] != codigo_trabajador_aprobador:
                raise ValidationError(
                    detail="No tiene permiso para aprobar esta solicitud",
                    internal_code="APROBACION_UNAUTHORIZED"
                )
            
            if aprobacion_actual['estado'] != 'P':
                raise ValidationError(
                    detail="Esta aprobación ya fue procesada",
                    internal_code="APROBACION_ALREADY_PROCESSED"
                )
            
            # 2. Actualizar aprobación
            # Aceptar tanto objeto Pydantic como diccionario
            if isinstance(aprobacion_data, dict):
                observacion = aprobacion_data.get('observacion')
                ip_dispositivo = aprobacion_data.get('ip_dispositivo')
                estado = aprobacion_data.get('estado', 'A')
            else:
                observacion = aprobacion_data.observacion
                ip_dispositivo = aprobacion_data.ip_dispositivo
                estado = getattr(aprobacion_data, 'estado', 'A')
            
            params = (
                estado,  # estado
                observacion,
                codigo_trabajador_aprobador,
                ip_dispositivo,
                id_aprobacion
            )
            
            resultado = execute_update(UPDATE_APROBACION, params)
            
            if not resultado or resultado.get('rows_affected', 0) == 0:
                raise ServiceError(
                    status_code=500,
                    detail="Error al actualizar la aprobación",
                    internal_code="APROBACION_UPDATE_ERROR"
                )
            
            # 3. Verificar si hay más niveles pendientes
            aprobaciones_actualizadas = await VacacionesPermisosService.obtener_aprobaciones(id_solicitud)
            aprobaciones_pendientes = [a for a in aprobaciones_actualizadas if a['estado'] == 'P']
            
            solicitud_completa = len(aprobaciones_pendientes) == 0
            
            # 4. Si es el último nivel, marcar solicitud como aprobada
            if solicitud_completa:
                execute_update(
                    UPDATE_SOLICITUD_ESTADO,
                    ('A', codigo_trabajador_aprobador, id_solicitud)
                )
                
                # Notificar al solicitante
                solicitud = await VacacionesPermisosService.obtener_solicitud(id_solicitud)
                await VacacionesPermisosService._crear_notificacion(
                    codigo_trabajador=solicitud['codigo_trabajador'],
                    id_solicitud=id_solicitud,
                    tipo_notificacion='A',
                    titulo="Solicitud Aprobada",
                    mensaje=f"Su solicitud de {'vacaciones' if solicitud['tipo_solicitud'] == 'V' else 'permiso'} ha sido aprobada",
                    prioridad='N'
                )
            else:
                # Notificar siguiente aprobador (notificación en bandeja + push notification)
                siguiente_aprobacion = aprobaciones_pendientes[0]
                
                # 1. Crear notificación en bandeja
                await VacacionesPermisosService._crear_notificacion(
                    codigo_trabajador=siguiente_aprobacion['codigo_trabajador_aprueba'],
                    id_solicitud=id_solicitud,
                    tipo_notificacion='N',
                    titulo="Nueva Solicitud Pendiente",
                    mensaje=f"Tiene una solicitud pendiente de aprobación (Nivel {siguiente_aprobacion['nivel']})",
                    prioridad='A'
                )
                
                # 2. Enviar notificación push al siguiente nivel
                try:
                    # Obtener información del solicitante para la notificación
                    solicitud = await VacacionesPermisosService.obtener_solicitud(id_solicitud)
                    trabajador = execute_query(
                        SELECT_TRABAJADOR_BY_CODIGO,
                        (solicitud['codigo_trabajador'],)
                    )
                    nombre_trabajador = trabajador[0].get('nombre_completo', 'Trabajador') if trabajador else 'Trabajador'
                    
                    from app.services.notificaciones_service import NotificacionesService
                    resultado_notif = await NotificacionesService.enviar_notificacion_siguiente_nivel(
                        id_solicitud=id_solicitud,
                        codigo_trabajador_solicitante=solicitud['codigo_trabajador'],
                        nombre_trabajador_solicitante=nombre_trabajador,
                        nivel_siguiente=siguiente_aprobacion['nivel'],
                        codigo_trabajador_aprobador_siguiente=siguiente_aprobacion['codigo_trabajador_aprueba'],
                        tipo_solicitud=solicitud.get('tipo_solicitud')
                    )
                    
                    if resultado_notif.get('enviado'):
                        logger.info(
                            f"✅ Notificación push enviada al nivel {siguiente_aprobacion['nivel']} "
                            f"para solicitud {id_solicitud}: {resultado_notif.get('success_count', 0)} dispositivos"
                        )
                    else:
                        logger.warning(
                            f"⚠️ No se pudo enviar notificación push al nivel {siguiente_aprobacion['nivel']} "
                            f"para solicitud {id_solicitud}: {resultado_notif.get('mensaje', 'Error desconocido')}"
                        )
                except Exception as notif_error:
                    # No fallar la aprobación si falla la notificación push
                    logger.exception(
                        f"❌ Error crítico enviando notificación push al siguiente nivel (no crítico para aprobación): {str(notif_error)}"
                    )
            
            BaseService.log_operation_success("Aprobación de solicitud", id_solicitud)
            
            return {
                'message': 'Solicitud aprobada exitosamente',
                'id_solicitud': id_solicitud,
                'id_aprobacion': id_aprobacion,
                'nivel': aprobacion_actual['nivel'],
                'estado': 'A',
                'solicitud_completa': solicitud_completa
            }
            
        except (NotFoundError, ValidationError):
            raise
        except Exception as e:
            logger.exception(f"Error aprobando solicitud {id_solicitud}: {str(e)}")
            raise ServiceError(
                status_code=500,
                detail="Error al aprobar solicitud",
                internal_code="APROBACION_APPROVE_UNEXPECTED_ERROR"
            )

    @staticmethod
    @BaseService.handle_service_errors
    async def rechazar_solicitud(
        id_solicitud: int,
        id_aprobacion: int,
        aprobacion_data: AprobacionUpdate,
        codigo_trabajador_aprobador: str
    ) -> Dict[str, Any]:
        """
        Rechaza una solicitud en un nivel específico.
        
        Args:
            id_solicitud: ID de la solicitud
            id_aprobacion: ID del registro de aprobación
            aprobacion_data: Datos de la aprobación (debe incluir observación)
            codigo_trabajador_aprobador: Código del aprobador
            
        Returns:
            Dict: Resultado del rechazo
        """
        try:
            # Validaciones similares a aprobar
            aprobaciones = await VacacionesPermisosService.obtener_aprobaciones(id_solicitud)
            aprobacion_actual = next(
                (a for a in aprobaciones if a['id_aprobacion'] == id_aprobacion),
                None
            )
            
            if not aprobacion_actual:
                raise NotFoundError(detail="Aprobación no encontrada")
            
            if aprobacion_actual['codigo_trabajador_aprueba'] != codigo_trabajador_aprobador:
                raise ValidationError(detail="No tiene permiso para rechazar esta solicitud")
            
            if aprobacion_actual['estado'] != 'P':
                raise ValidationError(detail="Esta aprobación ya fue procesada")
            
            # Aceptar tanto objeto Pydantic como diccionario
            if isinstance(aprobacion_data, dict):
                observacion = aprobacion_data.get('observacion')
                ip_dispositivo = aprobacion_data.get('ip_dispositivo')
                estado = aprobacion_data.get('estado', 'R')
            else:
                observacion = aprobacion_data.observacion
                ip_dispositivo = aprobacion_data.ip_dispositivo
                estado = getattr(aprobacion_data, 'estado', 'R')
            
            if not observacion:
                raise ValidationError(detail="Debe proporcionar una observación al rechazar")
            
            # Actualizar aprobación
            params = (
                estado,  # estado
                observacion,
                codigo_trabajador_aprobador,
                ip_dispositivo,
                id_aprobacion
            )
            
            resultado = execute_update(UPDATE_APROBACION, params)
            
            if not resultado or resultado.get('rows_affected', 0) == 0:
                raise ServiceError(
                    status_code=500,
                    detail="Error al actualizar la aprobación",
                    internal_code="APROBACION_UPDATE_ERROR"
                )
            
            # Marcar solicitud como rechazada
            execute_update(
                UPDATE_SOLICITUD_ESTADO,
                ('R', codigo_trabajador_aprobador, id_solicitud)
            )
            
            # Notificar al solicitante
            solicitud = await VacacionesPermisosService.obtener_solicitud(id_solicitud)
            await VacacionesPermisosService._crear_notificacion(
                codigo_trabajador=solicitud['codigo_trabajador'],
                id_solicitud=id_solicitud,
                tipo_notificacion='R',
                titulo="Solicitud Rechazada",
                mensaje=f"Su solicitud ha sido rechazada. Motivo: {observacion}",
                prioridad='A'
            )
            
            BaseService.log_operation_success("Rechazo de solicitud", id_solicitud)
            
            return {
                'message': 'Solicitud rechazada',
                'id_solicitud': id_solicitud,
                'id_aprobacion': id_aprobacion,
                'nivel': aprobacion_actual['nivel'],
                'estado': 'R'
            }
            
        except (NotFoundError, ValidationError):
            raise
        except Exception as e:
            logger.exception(f"Error rechazando solicitud {id_solicitud}: {str(e)}")
            raise ServiceError(
                status_code=500,
                detail="Error al rechazar solicitud",
                internal_code="APROBACION_REJECT_UNEXPECTED_ERROR"
            )

    # ============================================
    # MÉTODOS PRIVADOS - FLUJO DE APROBACIÓN
    # ============================================

    @staticmethod
    async def _crear_flujo_aprobacion(
        id_solicitud: int,
        tipo_solicitud: str,
        codigo_permiso: Optional[str],
        codigo_trabajador: str,
        dias_solicitados: Decimal,
        modo: str = 'D',
    ) -> None:
        """
        Crea el flujo de aprobación para una solicitud.
        
        Proceso:
        1. Obtener información del trabajador (área, sección, cargo)
        2. Determinar configuración de flujo aplicable
        3. Obtener jerarquía de aprobadores
        4. Aplicar sustitutos si aplica
        5. Crear registros de aprobación
        6. Crear notificaciones
        """
        try:
            # 1. Obtener información del trabajador
            trabajador = execute_query(
                SELECT_TRABAJADOR_BY_CODIGO,
                (codigo_trabajador,)
            )
            
            if not trabajador:
                raise NotFoundError(
                    detail=f"Trabajador {codigo_trabajador} no encontrado",
                    internal_code="TRABAJADOR_NOT_FOUND"
                )
            
            trabajador_info = trabajador[0]
            codigo_area = trabajador_info.get('codigo_area')
            codigo_seccion = trabajador_info.get('codigo_seccion')
            codigo_cargo = trabajador_info.get('codigo_cargo')

            # Permisos por hora (0 días en BD): usar mínimo 1 solo para matchear reglas de flujo
            dias_para_flujo = Decimal('1') if modo == 'H' else dias_solicitados
            
            # 2. Determinar configuración de flujo
            config_params = (
                tipo_solicitud,
                codigo_permiso, codigo_permiso,
                codigo_area, codigo_area,
                codigo_seccion, codigo_seccion,
                codigo_cargo, codigo_cargo,
                dias_para_flujo, dias_para_flujo,
                dias_para_flujo, dias_para_flujo,
            )
            
            config_flujo = execute_query(
                SELECT_CONFIG_FLUJO_APLICABLE,
                config_params
            )
            
            if not config_flujo:
                raise ServiceError(
                    status_code=500,
                    detail="No se encontró configuración de flujo aplicable",
                    internal_code="CONFIG_FLUJO_NOT_FOUND"
                )
            
            config = config_flujo[0]
            niveles_requeridos = config['niveles_requeridos']
            
            # 3. Obtener jerarquía de aprobadores
            jerarquia_params = (
                codigo_area, codigo_area,
                codigo_seccion, codigo_seccion,
                codigo_cargo, codigo_cargo
            )
            
            jerarquia = execute_query(
                SELECT_JERARQUIA_APROBADORES,
                jerarquia_params
            )
            
            if not jerarquia or len(jerarquia) < niveles_requeridos:
                # Construir mensaje descriptivo
                encontrados = len(jerarquia) if jerarquia else 0
                mensaje = (
                    f"No se encontraron suficientes aprobadores en la jerarquía. "
                    f"Requeridos: {niveles_requeridos}, Encontrados: {encontrados}. "
                    f"Área: {codigo_area or 'Todas'}, Sección: {codigo_seccion or 'Todas'}, "
                    f"Cargo: {codigo_cargo or 'Todos'}. "
                    f"Por favor, configure la jerarquía de aprobadores en el módulo de administración."
                )
                raise ServiceError(
                    status_code=500,
                    detail=mensaje,
                    internal_code="JERARQUIA_INCOMPLETA"
                )
            
            # 4. Crear registros de aprobación para los niveles requeridos
            for i in range(niveles_requeridos):
                nivel_info = jerarquia[i]
                codigo_aprobador = nivel_info['codigo_trabajador_aprobador']
                
                # Verificar sustitutos
                sustituto = execute_query(
                    SELECT_SUSTITUTO_ACTIVO,
                    (codigo_aprobador,)
                )
                
                if sustituto:
                    codigo_aprobador = sustituto[0]['codigo_trabajador_sustituto']
                
                # Crear registro de aprobación
                nivel = nivel_info['nivel_jerarquico']
                params = (
                    id_solicitud,
                    nivel,
                    codigo_aprobador,
                    'P',  # Pendiente
                    None,  # observacion
                    None,  # fecha
                    None,  # usuario
                    None,  # ip_dispositivo
                    datetime.now()  # fecha_notificado
                )
                
                execute_insert(INSERT_APROBACION, params)
                
                # Crear notificación solo para el primer nivel
                if i == 0:
                    await VacacionesPermisosService._crear_notificacion(
                        codigo_trabajador=codigo_aprobador,
                        id_solicitud=id_solicitud,
                        tipo_notificacion='N',
                        titulo="Nueva Solicitud Pendiente",
                        mensaje=f"Tiene una solicitud pendiente de aprobación (Nivel {nivel})",
                        prioridad='A'
                    )
            
            logger.info(f"Flujo de aprobación creado para solicitud {id_solicitud} con {niveles_requeridos} niveles")
            
        except Exception as e:
            logger.exception(f"Error creando flujo de aprobación: {str(e)}")
            raise

    @staticmethod
    async def _crear_notificacion(
        codigo_trabajador: str,
        id_solicitud: int,
        tipo_notificacion: str,
        titulo: str,
        mensaje: str,
        prioridad: str = 'N'
    ) -> None:
        """Crea una notificación"""
        try:
            params = (
                codigo_trabajador,
                id_solicitud,
                tipo_notificacion,
                titulo,
                mensaje,
                f"/solicitudes/{id_solicitud}",  # accion_url
                prioridad
            )
            execute_insert(INSERT_NOTIFICACION, params)
        except Exception as e:
            logger.error(f"Error creando notificación: {str(e)}")
            # No lanzar excepción, las notificaciones no son críticas

    # ============================================
    # MÉTODOS DE NOTIFICACIONES
    # ============================================

    @staticmethod
    @BaseService.handle_service_errors
    async def obtener_notificaciones(
        codigo_trabajador: str,
        leido: Optional[str] = None,
        page: int = 1,
        limit: int = 20
    ) -> Dict[str, Any]:
        """Obtiene notificaciones de un trabajador con paginación"""
        try:
            if page < 1:
                page = 1
            if limit < 1 or limit > 100:
                limit = 20
            
            offset = (page - 1) * limit
            limit_end = offset + limit
            
            params = (
                codigo_trabajador,
                leido, leido,
                offset + 1, limit_end
            )
            
            items = execute_query(SELECT_NOTIFICACIONES_PAGINATED, params)
            
            # Obtener total y no leídas
            count_result = execute_query(COUNT_NOTIFICACIONES, (codigo_trabajador,))
            total = count_result[0]['total'] if count_result else 0
            no_leidas = count_result[0]['no_leidas'] if count_result else 0
            
            pages = math.ceil(total / limit) if total > 0 else 0
            
            return {
                'items': items,
                'total': total,
                'page': page,
                'limit': limit,
                'pages': pages,
                'no_leidas': no_leidas
            }
            
        except Exception as e:
            logger.exception(f"Error obteniendo notificaciones: {str(e)}")
            raise ServiceError(
                status_code=500,
                detail="Error al obtener notificaciones",
                internal_code="NOTIFICACIONES_GET_ERROR"
            )

    @staticmethod
    @BaseService.handle_service_errors
    async def marcar_notificacion_leida(id_notificacion: int) -> Dict[str, Any]:
        """Marca una notificación como leída"""
        try:
            resultado = execute_update(UPDATE_NOTIFICACION_LEIDA, (id_notificacion,))
            
            if not resultado or resultado.get('rows_affected', 0) == 0:
                raise NotFoundError(
                    detail="Notificación no encontrada o ya está leída",
                    internal_code="NOTIFICACION_NOT_FOUND"
                )
            
            return resultado
            
        except NotFoundError:
            raise
        except Exception as e:
            logger.exception(f"Error marcando notificación como leída: {str(e)}")
            raise ServiceError(
                status_code=500,
                detail="Error al marcar notificación como leída",
                internal_code="NOTIFICACION_UPDATE_ERROR"
            )

    # ============================================
    # MÉTODOS ADMINISTRATIVOS - CONFIGURACIÓN DE FLUJO
    # ============================================

    @staticmethod
    @BaseService.handle_service_errors
    async def crear_config_flujo(
        config_data: Dict[str, Any],
        usuario_registro: str
    ) -> Dict[str, Any]:
        """Crea una nueva configuración de flujo"""
        try:
            # Validar y truncar campos según los límites de la BD
            # tipo_solicitud: char(1)
            # codigo_permiso: char(10)
            # codigo_area, codigo_seccion, codigo_cargo: char(4)
            # usuario_registro: char(8)
            # descripcion: varchar(200)
            
            def truncate_string(value: Optional[str], max_length: int) -> Optional[str]:
                """Trunca un string al máximo de caracteres permitido"""
                if value is None:
                    return None
                return value[:max_length].strip() if len(value) > max_length else value.strip()
            
            params = (
                truncate_string(config_data.get('tipo_solicitud'), 1),
                truncate_string(config_data.get('codigo_permiso'), 10),
                truncate_string(config_data.get('codigo_area'), 4),
                truncate_string(config_data.get('codigo_seccion'), 4),
                truncate_string(config_data.get('codigo_cargo'), 4),
                config_data.get('dias_desde'),
                config_data.get('dias_hasta'),
                config_data.get('niveles_requeridos', 2),
                config_data.get('orden', 1),
                truncate_string(config_data.get('activo', 'S'), 1),
                config_data.get('fecha_desde', date.today()),
                config_data.get('fecha_hasta'),
                truncate_string(usuario_registro, 8),
                truncate_string(config_data.get('descripcion'), 200)
            )
            
            resultado = execute_insert(INSERT_CONFIG_FLUJO, params)
            BaseService.log_operation_success("Creación de configuración de flujo", resultado.get('id_config'))
            return resultado
            
        except Exception as e:
            logger.exception(f"Error creando configuración de flujo: {str(e)}")
            raise ServiceError(
                status_code=500,
                detail="Error al crear configuración de flujo",
                internal_code="CONFIG_FLUJO_CREATE_ERROR"
            )

    @staticmethod
    @BaseService.handle_service_errors
    async def listar_config_flujo() -> List[Dict[str, Any]]:
        """Lista todas las configuraciones de flujo"""
        try:
            return execute_query(SELECT_ALL_CONFIG_FLUJO)
        except Exception as e:
            logger.exception(f"Error listando configuraciones: {str(e)}")
            raise ServiceError(
                status_code=500,
                detail="Error al listar configuraciones de flujo",
                internal_code="CONFIG_FLUJO_LIST_ERROR"
            )

    @staticmethod
    @BaseService.handle_service_errors
    async def obtener_config_flujo(id_config: int) -> Dict[str, Any]:
        """Obtiene una configuración de flujo por ID"""
        try:
            resultado = execute_query(SELECT_CONFIG_FLUJO_BY_ID, (id_config,))
            if not resultado:
                raise NotFoundError(
                    detail=f"Configuración de flujo {id_config} no encontrada",
                    internal_code="CONFIG_FLUJO_NOT_FOUND"
                )
            return resultado[0]
        except NotFoundError:
            raise
        except Exception as e:
            logger.exception(f"Error obteniendo configuración de flujo: {str(e)}")
            raise ServiceError(
                status_code=500,
                detail="Error al obtener configuración de flujo",
                internal_code="CONFIG_FLUJO_GET_ERROR"
            )

    @staticmethod
    @BaseService.handle_service_errors
    async def actualizar_config_flujo(
        id_config: int,
        config_data: Dict[str, Any],
        usuario_modificacion: str
    ) -> Dict[str, Any]:
        """Actualiza una configuración de flujo"""
        try:
            # Verificar que existe
            await VacacionesPermisosService.obtener_config_flujo(id_config)
            
            # Validar y truncar campos según los límites de la BD
            def truncate_string(value: Optional[str], max_length: int) -> Optional[str]:
                """Trunca un string al máximo de caracteres permitido"""
                if value is None:
                    return None
                return value[:max_length].strip() if len(value) > max_length else value.strip()
            
            params = (
                truncate_string(config_data.get('tipo_solicitud'), 1),
                truncate_string(config_data.get('codigo_permiso'), 10),
                truncate_string(config_data.get('codigo_area'), 4),
                truncate_string(config_data.get('codigo_seccion'), 4),
                truncate_string(config_data.get('codigo_cargo'), 4),
                config_data.get('dias_desde'),
                config_data.get('dias_hasta'),
                config_data.get('niveles_requeridos'),
                config_data.get('orden'),
                truncate_string(config_data.get('activo'), 1),
                config_data.get('fecha_desde'),
                config_data.get('fecha_hasta'),
                truncate_string(config_data.get('descripcion'), 200),
                id_config
            )
            
            resultado = execute_update(UPDATE_CONFIG_FLUJO, params)
            
            if not resultado or resultado.get('rows_affected', 0) == 0:
                raise NotFoundError(
                    detail=f"Configuración de flujo {id_config} no encontrada",
                    internal_code="CONFIG_FLUJO_UPDATE_NOT_FOUND"
                )
            
            BaseService.log_operation_success("Actualización de configuración de flujo", id_config)
            return await VacacionesPermisosService.obtener_config_flujo(id_config)
            
        except (NotFoundError, ValidationError):
            raise
        except Exception as e:
            logger.exception(f"Error actualizando configuración de flujo: {str(e)}")
            raise ServiceError(
                status_code=500,
                detail="Error al actualizar configuración de flujo",
                internal_code="CONFIG_FLUJO_UPDATE_ERROR"
            )

    @staticmethod
    @BaseService.handle_service_errors
    async def eliminar_config_flujo(id_config: int) -> Dict[str, Any]:
        """Elimina una configuración de flujo"""
        try:
            # Verificar que existe
            config = await VacacionesPermisosService.obtener_config_flujo(id_config)
            
            # Eliminar
            resultado = execute_update(DELETE_CONFIG_FLUJO, (id_config,))
            
            if not resultado or resultado.get('rows_affected', 0) == 0:
                raise NotFoundError(
                    detail=f"Configuración de flujo {id_config} no encontrada",
                    internal_code="CONFIG_FLUJO_DELETE_NOT_FOUND"
                )
            
            BaseService.log_operation_success("Eliminación de configuración de flujo", id_config)
            return {'message': f'Configuración de flujo {id_config} eliminada exitosamente', 'id_config': id_config}
            
        except NotFoundError:
            raise
        except Exception as e:
            logger.exception(f"Error eliminando configuración de flujo: {str(e)}")
            raise ServiceError(
                status_code=500,
                detail="Error al eliminar configuración de flujo",
                internal_code="CONFIG_FLUJO_DELETE_ERROR"
            )

    # ============================================
    # MÉTODOS ADMINISTRATIVOS - JERARQUÍA
    # ============================================

    @staticmethod
    @BaseService.handle_service_errors
    async def crear_jerarquia(
        jerarquia_data: Dict[str, Any],
        usuario_registro: str
    ) -> Dict[str, Any]:
        """Crea una nueva jerarquía"""
        try:
            # Validar y truncar campos según los límites de la BD
            # codigo_area, codigo_seccion, codigo_cargo: char(4)
            # codigo_trabajador_aprobador: char(8)
            # usuario_registro: char(8)
            
            def truncate_string(value: Optional[str], max_length: int) -> Optional[str]:
                """Trunca un string al máximo de caracteres permitido"""
                if value is None:
                    return None
                return value[:max_length].strip() if len(value) > max_length else value.strip()
            
            params = (
                truncate_string(jerarquia_data.get('codigo_area'), 4),
                truncate_string(jerarquia_data.get('codigo_seccion'), 4),
                truncate_string(jerarquia_data.get('codigo_cargo'), 4),
                truncate_string(jerarquia_data.get('codigo_trabajador_aprobador'), 8),
                jerarquia_data.get('tipo_relacion'),
                jerarquia_data.get('nivel_jerarquico'),
                jerarquia_data.get('activo', 'S'),
                jerarquia_data.get('fecha_desde', date.today()),
                jerarquia_data.get('fecha_hasta'),
                truncate_string(usuario_registro, 8),
                jerarquia_data.get('descripcion')  # varchar(200) - no necesita truncamiento aquí
            )
            
            resultado = execute_insert(INSERT_JERARQUIA, params)
            BaseService.log_operation_success("Creación de jerarquía", resultado.get('id_jerarquia'))
            return resultado
            
        except Exception as e:
            logger.exception(f"Error creando jerarquía: {str(e)}")
            raise ServiceError(
                status_code=500,
                detail="Error al crear jerarquía",
                internal_code="JERARQUIA_CREATE_ERROR"
            )

    @staticmethod
    @BaseService.handle_service_errors
    async def obtener_jerarquia(id_jerarquia: int) -> Dict[str, Any]:
        """Obtiene una jerarquía por ID"""
        try:
            resultado = execute_query(SELECT_JERARQUIA_BY_ID, (id_jerarquia,))
            if not resultado:
                raise NotFoundError(
                    detail=f"Jerarquía {id_jerarquia} no encontrada",
                    internal_code="JERARQUIA_NOT_FOUND"
                )
            return resultado[0]
        except NotFoundError:
            raise
        except Exception as e:
            logger.exception(f"Error obteniendo jerarquía: {str(e)}")
            raise ServiceError(
                status_code=500,
                detail="Error al obtener jerarquía",
                internal_code="JERARQUIA_GET_ERROR"
            )

    @staticmethod
    @BaseService.handle_service_errors
    async def actualizar_jerarquia(
        id_jerarquia: int,
        jerarquia_data: Dict[str, Any],
        usuario_modificacion: str
    ) -> Dict[str, Any]:
        """Actualiza una jerarquía"""
        try:
            # Verificar que existe
            await VacacionesPermisosService.obtener_jerarquia(id_jerarquia)
            
            # Validar y truncar campos según los límites de la BD
            def truncate_string(value: Optional[str], max_length: int) -> Optional[str]:
                """Trunca un string al máximo de caracteres permitido"""
                if value is None:
                    return None
                return value[:max_length].strip() if len(value) > max_length else value.strip()
            
            params = (
                truncate_string(jerarquia_data.get('codigo_area'), 4),
                truncate_string(jerarquia_data.get('codigo_seccion'), 4),
                truncate_string(jerarquia_data.get('codigo_cargo'), 4),
                truncate_string(jerarquia_data.get('codigo_trabajador_aprobador'), 8),
                jerarquia_data.get('tipo_relacion'),
                jerarquia_data.get('nivel_jerarquico'),
                truncate_string(jerarquia_data.get('activo'), 1),
                jerarquia_data.get('fecha_desde'),
                jerarquia_data.get('fecha_hasta'),
                truncate_string(jerarquia_data.get('descripcion'), 200),
                id_jerarquia
            )
            
            resultado = execute_update(UPDATE_JERARQUIA, params)
            
            if not resultado or resultado.get('rows_affected', 0) == 0:
                raise NotFoundError(
                    detail=f"Jerarquía {id_jerarquia} no encontrada",
                    internal_code="JERARQUIA_UPDATE_NOT_FOUND"
                )
            
            BaseService.log_operation_success("Actualización de jerarquía", id_jerarquia)
            return await VacacionesPermisosService.obtener_jerarquia(id_jerarquia)
            
        except (NotFoundError, ValidationError):
            raise
        except Exception as e:
            logger.exception(f"Error actualizando jerarquía: {str(e)}")
            raise ServiceError(
                status_code=500,
                detail="Error al actualizar jerarquía",
                internal_code="JERARQUIA_UPDATE_ERROR"
            )

    @staticmethod
    @BaseService.handle_service_errors
    async def eliminar_jerarquia(id_jerarquia: int) -> Dict[str, Any]:
        """Elimina una jerarquía"""
        try:
            # Verificar que existe
            jerarquia = await VacacionesPermisosService.obtener_jerarquia(id_jerarquia)
            
            # Eliminar
            resultado = execute_update(DELETE_JERARQUIA, (id_jerarquia,))
            
            if not resultado or resultado.get('rows_affected', 0) == 0:
                raise NotFoundError(
                    detail=f"Jerarquía {id_jerarquia} no encontrada",
                    internal_code="JERARQUIA_DELETE_NOT_FOUND"
                )
            
            BaseService.log_operation_success("Eliminación de jerarquía", id_jerarquia)
            return {'message': f'Jerarquía {id_jerarquia} eliminada exitosamente', 'id_jerarquia': id_jerarquia}
            
        except NotFoundError:
            raise
        except Exception as e:
            logger.exception(f"Error eliminando jerarquía: {str(e)}")
            raise ServiceError(
                status_code=500,
                detail="Error al eliminar jerarquía",
                internal_code="JERARQUIA_DELETE_ERROR"
            )

    @staticmethod
    @BaseService.handle_service_errors
    async def listar_jerarquia() -> List[Dict[str, Any]]:
        """Lista todas las jerarquías"""
        try:
            jerarquias = execute_query(SELECT_ALL_JERARQUIA)
            
            # Agregar nombres de aprobadores
            for j in jerarquias:
                trabajador = execute_query(
                    SELECT_TRABAJADOR_BY_CODIGO,
                    (j['codigo_trabajador_aprobador'],)
                )
                if trabajador:
                    j['nombre_aprobador'] = trabajador[0].get('nombre_completo')
            
            return jerarquias
            
        except Exception as e:
            logger.exception(f"Error listando jerarquías: {str(e)}")
            raise ServiceError(
                status_code=500,
                detail="Error al listar jerarquías",
                internal_code="JERARQUIA_LIST_ERROR"
            )

    # ============================================
    # MÉTODOS ADMINISTRATIVOS - SUSTITUTOS
    # ============================================

    @staticmethod
    @BaseService.handle_service_errors
    async def crear_sustituto(
        sustituto_data: Dict[str, Any],
        usuario_registro: str
    ) -> Dict[str, Any]:
        """Crea un nuevo sustituto"""
        try:
            # Validar y truncar campos según los límites de la BD
            # codigo_trabajador_titular, codigo_trabajador_sustituto: char(8)
            # usuario_registro: char(8)
            # motivo, observacion: varchar(200)
            
            def truncate_string(value: Optional[str], max_length: int) -> Optional[str]:
                """Trunca un string al máximo de caracteres permitido"""
                if value is None:
                    return None
                return value[:max_length].strip() if len(value) > max_length else value.strip()
            
            params = (
                truncate_string(sustituto_data.get('codigo_trabajador_titular'), 8),
                truncate_string(sustituto_data.get('codigo_trabajador_sustituto'), 8),
                sustituto_data.get('fecha_desde'),
                sustituto_data.get('fecha_hasta'),
                truncate_string(sustituto_data.get('motivo'), 200),
                truncate_string(sustituto_data.get('observacion'), 200),
                truncate_string(usuario_registro, 8)
            )
            
            resultado = execute_insert(INSERT_SUSTITUTO, params)
            BaseService.log_operation_success("Creación de sustituto", resultado.get('id_sustituto'))
            return resultado
            
        except Exception as e:
            logger.exception(f"Error creando sustituto: {str(e)}")
            raise ServiceError(
                status_code=500,
                detail="Error al crear sustituto",
                internal_code="SUSTITUTO_CREATE_ERROR"
            )

    @staticmethod
    @BaseService.handle_service_errors
    async def listar_sustitutos() -> List[Dict[str, Any]]:
        """Lista todos los sustitutos"""
        try:
            sustitutos = execute_query(SELECT_ALL_SUSTITUTOS)
            
            # Agregar nombres
            for s in sustitutos:
                titular = execute_query(
                    SELECT_TRABAJADOR_BY_CODIGO,
                    (s['codigo_trabajador_titular'],)
                )
                sustituto = execute_query(
                    SELECT_TRABAJADOR_BY_CODIGO,
                    (s['codigo_trabajador_sustituto'],)
                )
                if titular:
                    s['nombre_titular'] = titular[0].get('nombre_completo')
                if sustituto:
                    s['nombre_sustituto'] = sustituto[0].get('nombre_completo')
            
            return sustitutos
            
        except Exception as e:
            logger.exception(f"Error listando sustitutos: {str(e)}")
            raise ServiceError(
                status_code=500,
                detail="Error al listar sustitutos",
                internal_code="SUSTITUTO_LIST_ERROR"
            )

    # ============================================
    # MÉTODOS ADMINISTRATIVOS - ESTADÍSTICAS
    # ============================================

    @staticmethod
    @BaseService.handle_service_errors
    async def obtener_estadisticas(
        fecha_desde: Optional[date] = None,
        fecha_hasta: Optional[date] = None
    ) -> Dict[str, Any]:
        """Obtiene estadísticas del sistema"""
        try:
            from app.db.queries import SELECT_ESTADISTICAS_SOLICITUDES, SELECT_SOLICITUDES_POR_MES
            
            # Estadísticas generales
            params = (fecha_desde, fecha_desde, fecha_hasta, fecha_hasta)
            estadisticas = execute_query(SELECT_ESTADISTICAS_SOLICITUDES, params)
            
            # Solicitudes por mes
            por_mes = execute_query(SELECT_SOLICITUDES_POR_MES, params)
            
            resultado = {
                'total_solicitudes': estadisticas[0]['total_solicitudes'] if estadisticas else 0,
                'solicitudes_pendientes': estadisticas[0]['solicitudes_pendientes'] if estadisticas else 0,
                'solicitudes_aprobadas': estadisticas[0]['solicitudes_aprobadas'] if estadisticas else 0,
                'solicitudes_rechazadas': estadisticas[0]['solicitudes_rechazadas'] if estadisticas else 0,
                'solicitudes_por_tipo': {
                    'vacaciones': estadisticas[0]['solicitudes_vacaciones'] if estadisticas else 0,
                    'permisos': estadisticas[0]['solicitudes_permisos'] if estadisticas else 0
                },
                'solicitudes_por_mes': por_mes or []
            }
            
            return resultado
            
        except Exception as e:
            logger.exception(f"Error obteniendo estadísticas: {str(e)}")
            raise ServiceError(
                status_code=500,
                detail="Error al obtener estadísticas",
                internal_code="ESTADISTICAS_GET_ERROR"
            )

    @staticmethod
    @BaseService.handle_service_errors
    async def listar_saldos_vacaciones(
        codigo_area: Optional[str] = None,
        codigo_seccion: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Lista los saldos de vacaciones de todos los trabajadores"""
        try:
            from app.db.queries import SELECT_ALL_SALDOS_VACACIONES
            
            params = (codigo_area, codigo_area, codigo_seccion, codigo_seccion)
            saldos = execute_query(SELECT_ALL_SALDOS_VACACIONES, params)
            
            return saldos or []
            
        except Exception as e:
            logger.exception(f"Error listando saldos: {str(e)}")
            raise ServiceError(
                status_code=500,
                detail="Error al listar saldos de vacaciones",
                internal_code="SALDOS_LIST_ERROR"
            )

    # ============================================
    # MÉTODOS DE SALDOS Y CATÁLOGOS
    # ============================================

    @staticmethod
    @BaseService.handle_service_errors
    async def obtener_saldo_vacaciones(codigo_trabajador: str) -> Optional[Dict[str, Any]]:
        """Obtiene el saldo de vacaciones de un trabajador"""
        try:
            resultado = execute_query(SELECT_SALDO_VACACIONES, (codigo_trabajador,))
            
            if not resultado:
                return None
            
            saldo = resultado[0]
            
            # Obtener nombre del trabajador
            trabajador = execute_query(
                SELECT_TRABAJADOR_BY_CODIGO,
                (codigo_trabajador,)
            )
            if trabajador:
                saldo['nombre_trabajador'] = trabajador[0].get('nombre_completo')
            
            return saldo
            
        except Exception as e:
            logger.error(f"Error obteniendo saldo: {str(e)}")
            return None

    @staticmethod
    @BaseService.handle_service_errors
    async def obtener_catalogos() -> Dict[str, List[Dict[str, str]]]:
        """Obtiene todos los catálogos necesarios"""
        try:
            areas = execute_query(SELECT_CATALOGO_AREAS)
            secciones = execute_query(SELECT_CATALOGO_SECCIONES)
            cargos = execute_query(SELECT_CATALOGO_CARGOS)
            tipos_permiso = execute_query(SELECT_CATALOGO_TIPOS_PERMISO)
            if tipos_permiso:
                for item in tipos_permiso:
                    # ctiemp AS tiempo en SQL; aceptar claves legacy por compatibilidad
                    raw = item.get('tiempo') or item.get('ctiemp') or item.get('ctiempo')
                    item['tiempo'] = str(raw).strip().upper() if raw else 'D'
                    item.pop('ctiemp', None)
                    item.pop('ctiempo', None)
            
            return {
                'areas': areas or [],
                'secciones': secciones or [],
                'cargos': cargos or [],
                'tipos_permiso': tipos_permiso or []
            }
            
        except Exception as e:
            logger.exception(f"Error obteniendo catálogos: {str(e)}")
            raise ServiceError(
                status_code=500,
                detail="Error al obtener catálogos",
                internal_code="CATALOGOS_GET_ERROR"
            )

    @staticmethod
    @BaseService.handle_service_errors
    async def buscar_areas(
        codigo: Optional[str] = None,
        descripcion: Optional[str] = None,
        page: int = 1,
        limit: int = 20
    ) -> Dict:
        """Busca áreas con filtros y paginación"""
        try:
            offset = (page - 1) * limit
            codigo_param = f"%{codigo}%" if codigo else None
            descripcion_param = f"%{descripcion}%" if descripcion else None
            
            from app.db.queries import execute_query, SELECT_BUSCAR_AREAS, COUNT_BUSCAR_AREAS
            
            # Contar total
            count_params = (codigo, codigo_param, descripcion, descripcion_param)
            total_result = execute_query(COUNT_BUSCAR_AREAS, count_params)
            total = total_result[0]['total'] if total_result else 0
            
            # Obtener items (ROW_NUMBER usa offset y offset+limit)
            params = (codigo, codigo_param, descripcion, descripcion_param, offset, offset + limit)
            items = execute_query(SELECT_BUSCAR_AREAS, params)
            
            pages = math.ceil(total / limit) if total > 0 else 0
            
            return {
                'items': items or [],
                'total': total,
                'page': page,
                'limit': limit,
                'pages': pages
            }
        except Exception as e:
            logger.exception(f"Error buscando áreas: {str(e)}")
            raise ServiceError(
                status_code=500,
                detail="Error al buscar áreas",
                internal_code="BUSCAR_AREAS_ERROR"
            )

    @staticmethod
    @BaseService.handle_service_errors
    async def buscar_secciones(
        codigo: Optional[str] = None,
        descripcion: Optional[str] = None,
        page: int = 1,
        limit: int = 20
    ) -> Dict:
        """Busca secciones con filtros y paginación"""
        try:
            offset = (page - 1) * limit
            codigo_param = f"%{codigo}%" if codigo else None
            descripcion_param = f"%{descripcion}%" if descripcion else None
            
            from app.db.queries import execute_query, SELECT_BUSCAR_SECCIONES, COUNT_BUSCAR_SECCIONES
            
            # Contar total
            count_params = (codigo, codigo_param, descripcion, descripcion_param)
            total_result = execute_query(COUNT_BUSCAR_SECCIONES, count_params)
            total = total_result[0]['total'] if total_result else 0
            
            # Obtener items (ROW_NUMBER usa offset y offset+limit)
            params = (codigo, codigo_param, descripcion, descripcion_param, offset, offset + limit)
            items = execute_query(SELECT_BUSCAR_SECCIONES, params)
            
            pages = math.ceil(total / limit) if total > 0 else 0
            
            return {
                'items': items or [],
                'total': total,
                'page': page,
                'limit': limit,
                'pages': pages
            }
        except Exception as e:
            logger.exception(f"Error buscando secciones: {str(e)}")
            raise ServiceError(
                status_code=500,
                detail="Error al buscar secciones",
                internal_code="BUSCAR_SECCIONES_ERROR"
            )

    @staticmethod
    @BaseService.handle_service_errors
    async def buscar_cargos(
        codigo: Optional[str] = None,
        descripcion: Optional[str] = None,
        page: int = 1,
        limit: int = 20
    ) -> Dict:
        """Busca cargos con filtros y paginación"""
        try:
            offset = (page - 1) * limit
            codigo_param = f"%{codigo}%" if codigo else None
            descripcion_param = f"%{descripcion}%" if descripcion else None
            
            from app.db.queries import execute_query, SELECT_BUSCAR_CARGOS, COUNT_BUSCAR_CARGOS
            
            # Contar total
            count_params = (codigo, codigo_param, descripcion, descripcion_param)
            total_result = execute_query(COUNT_BUSCAR_CARGOS, count_params)
            total = total_result[0]['total'] if total_result else 0
            
            # Obtener items (ROW_NUMBER usa offset y offset+limit)
            params = (codigo, codigo_param, descripcion, descripcion_param, offset, offset + limit)
            items = execute_query(SELECT_BUSCAR_CARGOS, params)
            
            pages = math.ceil(total / limit) if total > 0 else 0
            
            return {
                'items': items or [],
                'total': total,
                'page': page,
                'limit': limit,
                'pages': pages
            }
        except Exception as e:
            logger.exception(f"Error buscando cargos: {str(e)}")
            raise ServiceError(
                status_code=500,
                detail="Error al buscar cargos",
                internal_code="BUSCAR_CARGOS_ERROR"
            )

    @staticmethod
    @BaseService.handle_service_errors
    async def buscar_trabajadores(
        codigo: Optional[str] = None,
        nombre: Optional[str] = None,
        codigo_area: Optional[str] = None,
        codigo_seccion: Optional[str] = None,
        codigo_cargo: Optional[str] = None,
        numero_dni: Optional[str] = None,
        page: int = 1,
        limit: int = 20
    ) -> Dict:
        """Busca trabajadores con filtros y paginación"""
        try:
            offset = (page - 1) * limit
            codigo_param = f"%{codigo}%" if codigo else None
            nombre_param = f"%{nombre}%" if nombre else None
            dni_param = f"%{numero_dni}%" if numero_dni else None
            
            from app.db.queries import execute_query, SELECT_BUSCAR_TRABAJADORES, COUNT_BUSCAR_TRABAJADORES
            
            # Contar total
            count_params = (
                codigo, codigo_param,
                nombre, nombre_param,
                codigo_area, codigo_area,
                codigo_seccion, codigo_seccion,
                codigo_cargo, codigo_cargo,
                numero_dni, dni_param
            )
            total_result = execute_query(COUNT_BUSCAR_TRABAJADORES, count_params)
            total = total_result[0]['total'] if total_result else 0
            
            # Obtener items (ROW_NUMBER usa offset y offset+limit)
            params = (
                codigo, codigo_param,
                nombre, nombre_param,
                codigo_area, codigo_area,
                codigo_seccion, codigo_seccion,
                codigo_cargo, codigo_cargo,
                numero_dni, dni_param,
                offset, offset + limit
            )
            items = execute_query(SELECT_BUSCAR_TRABAJADORES, params)
            
            pages = math.ceil(total / limit) if total > 0 else 0
            
            return {
                'items': items or [],
                'total': total,
                'page': page,
                'limit': limit,
                'pages': pages
            }
        except Exception as e:
            logger.exception(f"Error buscando trabajadores: {str(e)}")
            raise ServiceError(
                status_code=500,
                detail="Error al buscar trabajadores",
                internal_code="BUSCAR_TRABAJADORES_ERROR"
            )

    # ============================================
    # MÉTODOS DE TRABAJADORES Y CUMPLEAÑOS
    # ============================================

    @staticmethod
    @BaseService.handle_service_errors
    async def obtener_cumpleanos_hoy(
        page: int = 1,
        limit: int = 20
    ) -> Dict[str, Any]:
        """
        Obtiene la lista de trabajadores que cumplen años hoy con paginación.
        
        Args:
            page: Número de página
            limit: Elementos por página
            
        Returns:
            Dict: Resultado paginado con items y metadatos
        """
        try:
            # Validar parámetros
            if page < 1:
                page = 1
            if limit < 1 or limit > 100:
                limit = 20
            
            offset = (page - 1) * limit
            
            # Obtener total
            total_result = execute_query(COUNT_CUMPLEANOS_HOY)
            total = total_result[0]['total'] if total_result else 0
            
            # Obtener items (ROW_NUMBER usa offset y offset+limit)
            params = (offset, offset + limit)
            items = execute_query(SELECT_CUMPLEANOS_HOY, params)
            
            pages = math.ceil(total / limit) if total > 0 else 0
            
            return {
                'items': items or [],
                'total': total,
                'page': page,
                'limit': limit,
                'pages': pages
            }
        except Exception as e:
            logger.exception(f"Error obteniendo cumpleaños: {str(e)}")
            raise ServiceError(
                status_code=500,
                detail="Error al obtener lista de cumpleaños",
                internal_code="CUMPLEANOS_GET_ERROR"
            )

    @staticmethod
    @BaseService.handle_service_errors
    async def listar_trabajadores(
        codigo: Optional[str] = None,
        nombre: Optional[str] = None,
        codigo_area: Optional[str] = None,
        codigo_seccion: Optional[str] = None,
        codigo_cargo: Optional[str] = None,
        page: int = 1,
        limit: int = 20
    ) -> Dict[str, Any]:
        """
        Lista trabajadores con paginación y filtros.
        
        Args:
            codigo: Filtrar por código de trabajador
            nombre: Filtrar por nombre (búsqueda parcial)
            codigo_area: Filtrar por código de área
            codigo_seccion: Filtrar por código de sección
            codigo_cargo: Filtrar por código de cargo
            page: Número de página
            limit: Elementos por página
            
        Returns:
            Dict: Resultado paginado con items y metadatos
        """
        try:
            # Validar parámetros
            if page < 1:
                page = 1
            if limit < 1 or limit > 100:
                limit = 20
            
            offset = (page - 1) * limit
            
            # Preparar parámetros para búsqueda LIKE
            codigo_param = f"%{codigo}%" if codigo else None
            nombre_param = f"%{nombre}%" if nombre else None
            
            # Parámetros para COUNT
            count_params = (
                codigo, codigo_param,
                nombre, nombre_param,
                codigo_area, codigo_area,
                codigo_seccion, codigo_seccion,
                codigo_cargo, codigo_cargo
            )
            total_result = execute_query(COUNT_TRABAJADORES, count_params)
            total = total_result[0]['total'] if total_result else 0
            
            # Parámetros para SELECT (ROW_NUMBER usa offset y offset+limit)
            params = (
                codigo, codigo_param,
                nombre, nombre_param,
                codigo_area, codigo_area,
                codigo_seccion, codigo_seccion,
                codigo_cargo, codigo_cargo,
                offset, offset + limit
            )
            items = execute_query(SELECT_TRABAJADORES_PAGINATED, params)
            
            pages = math.ceil(total / limit) if total > 0 else 0
            
            return {
                'items': items or [],
                'total': total,
                'page': page,
                'limit': limit,
                'pages': pages
            }
        except Exception as e:
            logger.exception(f"Error listando trabajadores: {str(e)}")
            raise ServiceError(
                status_code=500,
                detail="Error al listar trabajadores",
                internal_code="TRABAJADORES_LIST_ERROR"
            )

    @staticmethod
    def _archivo_hex_a_base64(archivo_hex) -> str:
        """Convierte archivo en hexadecimal o bytes a base64."""
        import base64
        
        if isinstance(archivo_hex, bytes):
            pdf_bytes = archivo_hex
        elif isinstance(archivo_hex, str):
            hex_string = archivo_hex
            if hex_string.startswith('0x') or hex_string.startswith('0X'):
                hex_string = hex_string[2:]
            pdf_bytes = bytes.fromhex(hex_string)
        else:
            raise ValueError(f"Tipo de dato no soportado para archivo: {type(archivo_hex)}")
        
        return base64.b64encode(pdf_bytes).decode('utf-8')

    @staticmethod
    @BaseService.handle_service_errors
    async def obtener_boletas_pago(
        codigo_trabajador: str,
        anio: str,
        mes: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Obtiene boleta(s) de pago de un trabajador.
        - Si solo se pasa anio: devuelve todas las boletas de ese año (lista).
        - Si se pasa anio y mes: devuelve solo la boleta de ese año y mes (lista de 1 ítem).
        
        Args:
            codigo_trabajador: Código del trabajador
            anio: Año de la boleta (formato: YYYY)
            mes: Mes opcional (formato: MM). Si no se envía, se devuelven todas las boletas del año.
            
        Returns:
            Dict con clave "items" y lista de boletas, cada una con archivo_pdf_base64 y nombre_archivo.
            
        Raises:
            NotFoundError: Si no se encuentra ninguna boleta
            ServiceError: Si hay error al procesar algún archivo
        """
        try:
            from app.db.queries import execute_query, SELECT_BOLETA_PAGO, SELECT_BOLETAS_PAGO_POR_ANIO
            
            if mes is not None and mes != "":
                params = (codigo_trabajador, anio, mes)
                resultado = execute_query(SELECT_BOLETA_PAGO, params)
                filtro_desc = f"año {anio} y mes {mes}"
            else:
                params = (codigo_trabajador, anio)
                resultado = execute_query(SELECT_BOLETAS_PAGO_POR_ANIO, params)
                filtro_desc = f"año {anio}"
            
            if not resultado or len(resultado) == 0:
                raise NotFoundError(
                    detail=f"No se encontró boleta de pago para el {filtro_desc}. Verifique que exista en el sistema.",
                    internal_code="BOLETA_NOT_FOUND"
                )
            
            items = []
            for boleta in resultado:
                archivo_hex = boleta.get('archivo_pdf_hex')
                if not archivo_hex:
                    logger.warning(f"Boleta {boleta.get('mes')} sin archivo PDF, se omite")
                    continue
                
                try:
                    pdf_base64 = VacacionesPermisosService._archivo_hex_a_base64(archivo_hex)
                except Exception as e:
                    logger.error(f"Error convirtiendo archivo a base64: {str(e)}")
                    raise ServiceError(
                        status_code=500,
                        detail="Error al procesar el archivo PDF de la boleta",
                        internal_code="BOLETA_CONVERSION_ERROR"
                    )
                
                mes_val = boleta.get('mes') or ''
                semana_val = boleta.get('semana')
                nseman_val = boleta.get('nseman')
                if nseman_val is not None and str(nseman_val).strip() != '':
                    nombre_archivo = f"boleta_{codigo_trabajador}_{anio}_{mes_val}_s{semana_val or nseman_val}.pdf"
                else:
                    nombre_archivo = f"boleta_{codigo_trabajador}_{anio}_{mes_val}.pdf"
                items.append({
                    'codigo_trabajador': boleta['codigo_trabajador'],
                    'anio': boleta['anio'],
                    'mes': mes_val,
                    'nseman': str(nseman_val) if nseman_val is not None else None,
                    'semana': int(semana_val) if semana_val is not None else None,
                    'tipo_documento': boleta.get('tipo_documento'),
                    'archivo_pdf_base64': pdf_base64,
                    'nombre_archivo': nombre_archivo
                })
            
            if not items:
                raise NotFoundError(
                    detail=f"Las boletas para el {filtro_desc} no tienen archivo PDF asociado. Contacte al área de recursos humanos.",
                    internal_code="BOLETA_SIN_ARCHIVO"
                )
            
            return {'items': items}
            
        except (NotFoundError, ServiceError):
            raise
        except Exception as e:
            logger.exception(f"Error obteniendo boletas de pago: {str(e)}")
            raise ServiceError(
                status_code=500,
                detail="Error al obtener las boletas de pago",
                internal_code="BOLETA_GET_ERROR"
            )

    @staticmethod
    @BaseService.handle_service_errors
    async def obtener_documentos_pago(
        codigo_trabajador: str,
        anio: str,
        mes: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Obtiene documentos de pago (ctpdoc = 'O') del trabajador.
        - Solo anio: todos los documentos de pago del año.
        - anio + mes: solo documentos de ese año/mes.
        """
        try:
            from app.db.queries import execute_query, SELECT_DOCUMENTO_PAGO, SELECT_DOCUMENTO_PAGO_POR_ANIO

            if mes is not None and mes != "":
                params = (codigo_trabajador, anio, mes)
                resultado = execute_query(SELECT_DOCUMENTO_PAGO, params)
                filtro_desc = f"año {anio} y mes {mes}"
            else:
                params = (codigo_trabajador, anio)
                resultado = execute_query(SELECT_DOCUMENTO_PAGO_POR_ANIO, params)
                filtro_desc = f"año {anio}"

            if not resultado or len(resultado) == 0:
                raise NotFoundError(
                    detail=f"No se encontró documento de pago para el {filtro_desc}. Verifique que exista en el sistema.",
                    internal_code="DOC_PAGO_NOT_FOUND"
                )

            items: List[Dict[str, Any]] = []
            for doc in resultado:
                archivo_hex = doc.get('archivo_pdf_hex')
                if not archivo_hex:
                    logger.warning(f"Documento de pago mes {doc.get('mes')} sin archivo PDF, se omite")
                    continue

                try:
                    pdf_base64 = VacacionesPermisosService._archivo_hex_a_base64(archivo_hex)
                except Exception as e:
                    logger.error(f"Error convirtiendo archivo a base64: {str(e)}")
                    raise ServiceError(
                        status_code=500,
                        detail="Error al procesar el archivo PDF del documento de pago",
                        internal_code="DOC_PAGO_CONVERSION_ERROR"
                    )

                mes_val = doc.get('mes') or ''
                semana_val = doc.get('semana')
                nseman_val = doc.get('nseman')
                if nseman_val is not None and str(nseman_val).strip() != '':
                    nombre_archivo = f"documento_pago_{codigo_trabajador}_{anio}_{mes_val}_s{semana_val or nseman_val}.pdf"
                else:
                    nombre_archivo = f"documento_pago_{codigo_trabajador}_{anio}_{mes_val}.pdf"

                items.append({
                    'codigo_trabajador': doc['codigo_trabajador'],
                    'anio': doc['anio'],
                    'mes': mes_val,
                    'nseman': str(nseman_val) if nseman_val is not None else None,
                    'semana': int(semana_val) if semana_val is not None else None,
                    'tipo_documento': doc.get('tipo_documento'),
                    'archivo_pdf_base64': pdf_base64,
                    'nombre_archivo': nombre_archivo
                })

            if not items:
                raise NotFoundError(
                    detail=f"Los documentos de pago para el {filtro_desc} no tienen archivo PDF asociado. Contacte al área de recursos humanos.",
                    internal_code="DOC_PAGO_SIN_ARCHIVO"
                )

            return {'items': items}

        except (NotFoundError, ServiceError):
            raise
        except Exception as e:
            logger.exception(f"Error obteniendo documentos de pago: {str(e)}")
            raise ServiceError(
                status_code=500,
                detail="Error al obtener los documentos de pago",
                internal_code="DOC_PAGO_GET_ERROR"
            )

    @staticmethod
    @BaseService.handle_service_errors
    async def obtener_certificados_cts(
        codigo_trabajador: str,
        anio: str
    ) -> Dict[str, Any]:
        """
        Obtiene todos los certificados CTS de un trabajador para un año.
        Normalmente hay 2 certificados por año (ej. mayo y noviembre).
        Si el año está en curso puede haber 1 registro parcial.
        
        Args:
            codigo_trabajador: Código del trabajador
            anio: Año del certificado (formato: YYYY)
            
        Returns:
            Dict con clave "items" y lista de certificados, cada uno con archivo_pdf_base64 y nombre_archivo.
            
        Raises:
            NotFoundError: Si no se encuentra ningún certificado
            ServiceError: Si hay error al procesar algún archivo
        """
        try:
            from app.db.queries import execute_query, SELECT_CERTIFICADOS_CTS
            
            params = (codigo_trabajador, anio)
            resultado = execute_query(SELECT_CERTIFICADOS_CTS, params)
            
            if not resultado or len(resultado) == 0:
                raise NotFoundError(
                    detail=f"No se encontró certificado CTS para el año {anio}. Verifique que exista en el sistema.",
                    internal_code="CERTIFICADO_CTS_NOT_FOUND"
                )
            
            items = []
            for idx, certificado in enumerate(resultado):
                archivo_hex = certificado.get('archivo_pdf_hex')
                if not archivo_hex:
                    logger.warning(f"Certificado CTS mes {certificado.get('mes')} sin archivo PDF, se omite")
                    continue
                
                try:
                    pdf_base64 = VacacionesPermisosService._archivo_hex_a_base64(archivo_hex)
                except Exception as e:
                    logger.error(f"Error convirtiendo archivo a base64: {str(e)}")
                    raise ServiceError(
                        status_code=500,
                        detail="Error al procesar el archivo PDF del certificado",
                        internal_code="CERTIFICADO_CONVERSION_ERROR"
                    )
                
                mes_val = certificado.get('mes')
                nseman_val = certificado.get('nseman')
                if mes_val:
                    nombre_archivo = f"certificado_cts_{codigo_trabajador}_{anio}_{mes_val}.pdf"
                else:
                    nombre_archivo = f"certificado_cts_{codigo_trabajador}_{anio}_{idx + 1}.pdf"
                
                items.append({
                    'codigo_trabajador': certificado['codigo_trabajador'],
                    'anio': certificado['anio'],
                    'mes': mes_val,
                    'nseman': str(nseman_val) if nseman_val is not None else None,
                    'tipo_documento': certificado.get('tipo_documento'),
                    'archivo_pdf_base64': pdf_base64,
                    'nombre_archivo': nombre_archivo
                })
            
            if not items:
                raise NotFoundError(
                    detail=f"Los certificados CTS para el año {anio} no tienen archivo PDF asociado. Contacte al área de recursos humanos.",
                    internal_code="CERTIFICADO_SIN_ARCHIVO"
                )
            
            return {'items': items}
            
        except (NotFoundError, ServiceError):
            raise
        except Exception as e:
            logger.exception(f"Error obteniendo certificados CTS: {str(e)}")
            raise ServiceError(
                status_code=500,
                detail="Error al obtener los certificados CTS",
                internal_code="CERTIFICADO_CTS_GET_ERROR"
            )

    @staticmethod
    @BaseService.handle_service_errors
    async def obtener_documentos_empresa() -> Dict[str, Any]:
        """
        Obtiene documentos de empresa (ctpdoc = 'E') desde pdocum00.
        """
        try:
            from app.db.queries import execute_query, SELECT_DOCUMENTOS_EMPRESA

            resultado = execute_query(SELECT_DOCUMENTOS_EMPRESA, ())
            if not resultado:
                raise NotFoundError(
                    detail="No se encontraron documentos de empresa.",
                    internal_code="DOC_EMPRESA_NOT_FOUND"
                )

            items: List[Dict[str, Any]] = []
            for idx, row in enumerate(resultado):
                archivo_hex = row.get('archivo_pdf_hex')
                if not archivo_hex:
                    logger.warning(f"Documento de empresa '{row.get('ddcocum')}' sin archivo PDF, se omite")
                    continue

                try:
                    pdf_base64 = VacacionesPermisosService._archivo_hex_a_base64(archivo_hex)
                except Exception as e:
                    logger.error(f"Error convirtiendo archivo a base64: {str(e)}")
                    raise ServiceError(
                        status_code=500,
                        detail="Error al procesar el archivo PDF de documentos de empresa",
                        internal_code="DOC_EMPRESA_CONVERSION_ERROR"
                    )

                nombre_archivo = f"documento_empresa_{idx + 1}.pdf"
                items.append({
                    'descripcion': row.get('ddcocum', ''),
                    'tipo_documento': row.get('tipo_documento'),
                    'archivo_pdf_base64': pdf_base64,
                    'nombre_archivo': nombre_archivo
                })

            if not items:
                raise NotFoundError(
                    detail="Los documentos de empresa no tienen archivo PDF asociado. Contacte al área correspondiente.",
                    internal_code="DOC_EMPRESA_SIN_ARCHIVO"
                )

            return {'items': items}

        except (NotFoundError, ServiceError):
            raise
        except Exception as e:
            logger.exception(f"Error obteniendo documentos de empresa: {str(e)}")
            raise ServiceError(
                status_code=500,
                detail="Error al obtener los documentos de empresa",
                internal_code="DOC_EMPRESA_GET_ERROR"
            )

    @staticmethod
    @BaseService.handle_service_errors
    async def obtener_avisos_empresa() -> Dict[str, Any]:
        """
        Obtiene avisos de empresa (ctpdoc = 'A') desde pdocum00.
        """
        try:
            from app.db.queries import execute_query, SELECT_AVISOS_EMPRESA

            resultado = execute_query(SELECT_AVISOS_EMPRESA, ())
            if not resultado:
                raise NotFoundError(
                    detail="No se encontraron avisos de empresa.",
                    internal_code="AVISO_EMPRESA_NOT_FOUND"
                )

            items: List[Dict[str, Any]] = []
            for idx, row in enumerate(resultado):
                archivo_hex = row.get('archivo_pdf_hex')
                if not archivo_hex:
                    logger.warning(f"Aviso de empresa '{row.get('ddcocum')}' sin archivo PDF, se omite")
                    continue

                try:
                    pdf_base64 = VacacionesPermisosService._archivo_hex_a_base64(archivo_hex)
                except Exception as e:
                    logger.error(f"Error convirtiendo archivo a base64: {str(e)}")
                    raise ServiceError(
                        status_code=500,
                        detail="Error al procesar el archivo PDF de avisos de empresa",
                        internal_code="AVISO_EMPRESA_CONVERSION_ERROR"
                    )

                nombre_archivo = f"aviso_empresa_{idx + 1}.pdf"
                items.append({
                    'descripcion': row.get('ddcocum', ''),
                    'tipo_documento': row.get('tipo_documento'),
                    'archivo_pdf_base64': pdf_base64,
                    'nombre_archivo': nombre_archivo
                })

            if not items:
                raise NotFoundError(
                    detail="Los avisos de empresa no tienen archivo PDF asociado. Contacte al área correspondiente.",
                    internal_code="AVISO_EMPRESA_SIN_ARCHIVO"
                )

            return {'items': items}

        except (NotFoundError, ServiceError):
            raise
        except Exception as e:
            logger.exception(f"Error obteniendo avisos de empresa: {str(e)}")
            raise ServiceError(
                status_code=500,
                detail="Error al obtener los avisos de empresa",
                internal_code="AVISO_EMPRESA_GET_ERROR"
            )

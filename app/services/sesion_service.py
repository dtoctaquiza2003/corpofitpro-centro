from sqlalchemy.orm import Session
from datetime import date
from ..models import SesionTerapia, PacientePaquete, Alerta, SesionTratamiento
from ..schemas.sesion import SesionCreate

def registrar_sesion(db: Session, sesion_data: SesionCreate, terapeuta_id: int) -> SesionTerapia:
    """
    Registra una nueva sesión de terapia.
    La lógica de descuento de paquetes se delega en el trigger de la BD (fn_trg_descontar_sesion).
    Este servicio solo crea la sesión, asigna el paquete activo (si existe) y genera alertas.
    """
    # 1. Crear la sesión (sin paquete asociado aún)
    nueva_sesion = SesionTerapia(
        paciente_id=sesion_data.paciente_id,
        terapeuta_id=terapeuta_id,
        fecha=sesion_data.fecha,
        hora_ingreso=sesion_data.hora_ingreso,
        hora_salida=sesion_data.hora_salida,
        escala_dolor_entrada=sesion_data.escala_dolor_entrada,
        escala_dolor_salida=sesion_data.escala_dolor_salida,
        paciente_paquete_id=None
    )
    db.add(nueva_sesion)
    db.flush()  # para obtener el id de la sesión antes del commit

    # 2. Buscar un paquete activo y no expirado del paciente
    paquete_activo = db.query(PacientePaquete).filter(
        PacientePaquete.paciente_id == sesion_data.paciente_id,
        PacientePaquete.estado == "ACTIVO",
        PacientePaquete.fecha_expiracion >= date.today()
    ).first()

    if paquete_activo:
        # Asignar el paquete a la sesión. El trigger se encargará de incrementar sesiones_usadas
        nueva_sesion.paciente_paquete_id = paquete_activo.id

    # 3. Registrar tratamientos adicionales (si se enviaron)
    if sesion_data.tratamientos:
        for tratamiento_id in sesion_data.tratamientos:
            st = SesionTratamiento(
                sesion_id=nueva_sesion.id,
                tratamiento_id=tratamiento_id
            )
            db.add(st)

    db.commit()
    db.refresh(nueva_sesion)

    # 4. Generar alertas por dolor
    alerta_generada = None

    # Buscar sesión anterior del mismo paciente
    sesion_anterior = db.query(SesionTerapia).filter(
        SesionTerapia.paciente_id == sesion_data.paciente_id,
        SesionTerapia.fecha < sesion_data.fecha
    ).order_by(SesionTerapia.fecha.desc()).first()

    # Alerta por aumento de dolor (>= 2 puntos)
    if sesion_anterior:
        aumento = sesion_data.escala_dolor_entrada - sesion_anterior.escala_dolor_salida
        if aumento >= 2:
            alerta_generada = Alerta(
                paciente_id=sesion_data.paciente_id,
                tipo="pain_increase",
                descripcion=f"Aumento de dolor: {sesion_anterior.escala_dolor_salida} → {sesion_data.escala_dolor_entrada}"
            )
            db.add(alerta_generada)

    # Alerta por dolor crítico (>= 8)
    if sesion_data.escala_dolor_entrada >= 8:
        alerta_generada = Alerta(
            paciente_id=sesion_data.paciente_id,
            tipo="high_pain",
            descripcion=f"Dolor crítico: {sesion_data.escala_dolor_entrada}/10"
        )
        db.add(alerta_generada)

    if alerta_generada:
        db.commit()

    return nueva_sesion
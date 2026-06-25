from sqlalchemy.orm import Session
from typing import List
from ..models import Alerta

def marcar_alerta_leida(db: Session, alerta_id: int) -> Alerta:
    """
    Marca una alerta como leída.
    """
    alerta = db.query(Alerta).filter(Alerta.id == alerta_id).first()
    if alerta:
        alerta.leida = True
        db.commit()
        db.refresh(alerta)
    return alerta

def obtener_alertas_paciente(db: Session, paciente_id: int, solo_no_leidas: bool = False) -> List[Alerta]:
    """
    Obtiene las alertas de un paciente específico.
    """
    query = db.query(Alerta).filter(Alerta.paciente_id == paciente_id)
    if solo_no_leidas:
        query = query.filter(Alerta.leida == False)
    return query.order_by(Alerta.fecha.desc()).all()
from dotenv import load_dotenv

# Cargar variables del archivo .env antes de importar servicios/routers
load_dotenv()

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from .routers import (
    auth_router,
    pacientes_router,
    sesiones_router,
    asistencias_router,
    paquetes_router,
    pagos_router,
    alertas_router,
    reportes_router,
    consultorios_router,
    usuarios_router,
    transferencias_router,
    diagnosticos_router,
    tratamiento_paciente_router,
    tipos_terapia_router,
    notificaciones_router,
    gimnasio_router,
    pacientes_compartidos_router,
    permisos_temporales_router,
)

# Crear la aplicación FastAPI
app = FastAPI(
    title="FisioControl API",
    description="API para gestión de clínicas de rehabilitación",
    version="1.0.0"
)

# Configurar CORS (permite peticiones desde cualquier origen - ajustar en producción)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],          # En producción, especificar los dominios permitidos
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Incluir los routers
app.include_router(auth_router)
app.include_router(pacientes_router)
app.include_router(sesiones_router)
app.include_router(asistencias_router)
#app.include_router(paquetes_router)
app.include_router(pagos_router)
app.include_router(alertas_router)
app.include_router(reportes_router)
#app.include_router(terapeutas_router)
app.include_router(consultorios_router)
app.include_router(usuarios_router)
app.include_router(transferencias_router)
app.include_router(diagnosticos_router)
app.include_router(tratamiento_paciente_router)
app.include_router(tipos_terapia_router)
app.include_router(notificaciones_router)
app.include_router(gimnasio_router)
app.include_router(pacientes_compartidos_router)
app.include_router(permisos_temporales_router)

# Endpoint de prueba
@app.get("/")
def root():
    return {"message": "FisioControl API funcionando correctamente"}

# Endpoint de health check
@app.get("/health")
async def health_check():
    # Debe ser extremadamente liviano: no tocar DB ni servicios externos.
    return {"status": "ok"}
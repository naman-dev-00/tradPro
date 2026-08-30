from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from src.config import settings
from src.database import Base, engine, verify_database_connection
from src.middleware.observability import ObservabilityMiddleware
from src.routes import health, auth, admin, strategies, indicators, rules, multi_series, replays, data_quality

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Enforce production cookie security constraints on startup
    settings.verify_security_settings()
    # Verify database connection safety on startup
    verify_database_connection()
    # Automatically initialize tables in the active database
    Base.metadata.create_all(bind=engine)
    yield

app = FastAPI(
    title="TradePro API",
    description="Backend API for TradePro strategy building, indicator analysis, and backtesting",
    version="1.0.0",
    lifespan=lifespan
)

# Enable CORS for Next.js frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Enable Request-ID and Observability Logging Middleware
app.add_middleware(ObservabilityMiddleware)

# Include routers
app.include_router(health.router)
app.include_router(auth.router)
app.include_router(admin.router)
app.include_router(strategies.router)
app.include_router(indicators.router)
app.include_router(rules.rules_router)
app.include_router(multi_series.router)
app.include_router(replays.router)
app.include_router(data_quality.router)

@app.get("/")
def read_root():
    return {"message": "Welcome to TradePro API. Access health at /health and docs at /docs."}

from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from src.database import Base, engine, verify_database_connection
from src.routes import health, strategies, indicators

@asynccontextmanager
async def lifespan(app: FastAPI):
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
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
app.include_router(health.router)
app.include_router(strategies.router)
app.include_router(indicators.router)

@app.get("/")
def read_root():
    return {"message": "Welcome to TradePro API. Access health at /health and docs at /docs."}

import datetime
import uuid
from sqlalchemy import Column, String, DateTime, JSON
from src.database import Base

class Strategy(Base):
    __tablename__ = "strategies"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String(255), nullable=False)
    description = Column(String(1024), nullable=True)
    timeframe = Column(String(50), nullable=False)
    candidate_selection_mode = Column(String(50), nullable=False, default="FIRST_ELIGIBLE")
    payload = Column(JSON, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.datetime.now(datetime.timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.datetime.now(datetime.timezone.utc), onupdate=lambda: datetime.datetime.now(datetime.timezone.utc))

    @property
    def action(self):
        return self.payload.get("action")

    @property
    def global_conditions(self):
        return self.payload.get("global_conditions")

    @property
    def candidate_conditions(self):
        return self.payload.get("candidate_conditions")

import json
import logging
from src.database import SessionLocal, Base, engine
from src.models import Strategy

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("tradepro.seed")

EXAMPLE_STRATEGY = {
    "id": "7b5ef35b-1175-430c-ab23-f22287955c45",
    "name": "Nifty RSI Touch",
    "description": "Global: NIFTY price > EMA 200. Candidate: RSI < 40 AND price TOUCHES S1. Simulated paper trade action.",
    "timeframe": "15m",
    "candidate_selection_mode": "FIRST_ELIGIBLE",
    "global_conditions": {
        "type": "CONDITION",
        "lhs": {
            "indicator": "PRICE",
            "symbol": "NIFTY"
        },
        "operator": "GREATER_THAN",
        "rhs": {
            "type": "INDICATOR",
            "indicator": {
                "indicator": "EMA",
                "symbol": "NIFTY",
                "params": {"period": 200}
            }
        }
    },
    "candidate_conditions": {
        "type": "AND",
        "conditions": [
            {
                "type": "CONDITION",
                "lhs": {
                    "indicator": "RSI",
                    "symbol": "CANDIDATE",
                    "params": {"period": 14}
                },
                "operator": "LESS_THAN",
                "rhs": {
                    "type": "NUMBER",
                    "value": 40.0
                }
            },
            {
                "type": "CONDITION",
                "lhs": {
                    "indicator": "PRICE",
                    "symbol": "CANDIDATE"
                },
                "operator": "TOUCHES",
                "rhs": {
                    "type": "INDICATOR",
                    "indicator": {
                        "indicator": "PIVOT",
                        "symbol": "CANDIDATE",
                        "params": {"level": "S1"}
                    }
                }
            }
        ]
    },
    "action": {
        "type": "PAPER_TRADE",
        "risk_config": {
            "max_position_size": 100000.0,
            "stop_loss_pct": 2.5,
            "take_profit_pct": 5.0,
            "validity_window": 5
        }
    }
}

def seed_db():
    # Make sure tables exist
    Base.metadata.create_all(bind=engine)

    db = SessionLocal()
    try:
        strategy_id = EXAMPLE_STRATEGY["id"]
        existing = db.query(Strategy).filter(Strategy.id == strategy_id).first()
        if existing:
            logger.info("Database is already seeded with example strategy.")
            return

        db_strategy = Strategy(
            id=strategy_id,
            name=EXAMPLE_STRATEGY["name"],
            description=EXAMPLE_STRATEGY["description"],
            timeframe=EXAMPLE_STRATEGY["timeframe"],
            candidate_selection_mode=EXAMPLE_STRATEGY["candidate_selection_mode"],
            payload=EXAMPLE_STRATEGY
        )

        db.add(db_strategy)
        db.commit()
        logger.info("Successfully seeded database with example strategy blueprint.")
    except Exception as e:
        db.rollback()
        logger.error(f"Error seeding database: {e}")
        raise e
    finally:
        db.close()

if __name__ == "__main__":
    seed_db()

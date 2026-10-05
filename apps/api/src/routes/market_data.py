import logging
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from src.models import User
from src.auth.dependencies import get_current_user
from src.auth.rate_limiter import rate_limiter
from src.engine.market_data.contracts import (
    MarketDataReadinessResponse,
    MarketDataInstrument,
    MarketDataCandlesResponse,
    MarketDataDisabledError,
    MarketDataAuthenticationError,
    MarketDataRateLimitedError,
    MarketDataServiceUnavailableError,
    MarketDataValidationError,
    MarketDataForbiddenError,
)
from src.services.market_data_service import MarketDataService

logger = logging.getLogger("tradepro.market_data_routes")

router = APIRouter(prefix="/api/v1/market-data", tags=["market-data"])


@router.get("/readiness", response_model=MarketDataReadinessResponse)
def get_market_data_readiness(
    current_user: User = Depends(get_current_user),
):
    """
    Evaluates market data readiness with strictly zero external network calls
    and zero database mutations.
    """
    rate_limiter.check_rate_limit(
        f"market_data_readiness:{current_user.id}",
        max_requests=60,
        window_seconds=60,
    )
    return MarketDataService.get_readiness(current_user.id)


@router.get("/instruments", response_model=List[MarketDataInstrument])
def get_market_data_instruments(
    current_user: User = Depends(get_current_user),
):
    """
    Returns curated catalog of supported market data instruments.
    """
    rate_limiter.check_rate_limit(
        f"market_data_instruments:{current_user.id}",
        max_requests=60,
        window_seconds=60,
    )
    return MarketDataService.get_supported_instruments()


@router.get("/candles", response_model=MarketDataCandlesResponse)
def get_market_data_candles(
    response: Response,
    instrument_key: str = Query(..., description="Provider instrument token e.g. NSE_INDEX|Nifty 50"),
    timeframe: str = Query("5m", description="Candle timeframe e.g. 5m, 15m"),
    mode: str = Query("intraday", description="Acquisition mode: 'intraday' or 'historical'"),
    from_date: Optional[str] = Query(None, description="Start date YYYY-MM-DD for historical mode"),
    to_date: Optional[str] = Query(None, description="End date YYYY-MM-DD for historical mode"),
    current_user: User = Depends(get_current_user),
):
    """
    Acquires and normalizes completed provider candles for an authorized operator.
    Enforces network enablement, bounded requests, rate limits, and sanitizes errors.
    """
    rate_limiter.check_rate_limit(
        f"market_data_candles:{current_user.id}",
        max_requests=30,
        window_seconds=60,
    )

    try:
        result = MarketDataService.fetch_and_normalize_candles(
            user_id=current_user.id,
            instrument_key=instrument_key,
            timeframe=timeframe,
            mode=mode,
            from_date=from_date,
            to_date=to_date,
        )
        return result

    except MarketDataForbiddenError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))
    except MarketDataDisabledError as e:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(e))
    except MarketDataAuthenticationError as e:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(e))
    except MarketDataRateLimitedError as e:
        response.headers["Retry-After"] = str(e.retry_after)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=str(e),
            headers={"Retry-After": str(e.retry_after)},
        )
    except MarketDataValidationError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except MarketDataServiceUnavailableError as e:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(e))

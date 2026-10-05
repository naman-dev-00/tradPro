import os
import datetime
import logging
import urllib.parse
from decimal import Decimal
from typing import Callable, List, Optional
from src.engine.market_data.contracts import (
    SUPPORTED_MARKET_DATA_TIMEFRAMES,
    DEFAULT_UPSTOX_MARKET_DATA_BASE_URL,
    APPROVED_MARKET_DATA_HOSTS,
    MarketDataCandle,
    MarketDataProvenance,
    MarketDataReadinessResponse,
    MarketDataInstrument,
    MarketDataCandlesResponse,
    MarketDataValidationError,
    MarketDataForbiddenError,
)
from src.engine.market_data.adapter import (
    UpstoxMarketDataAdapter,
    evaluate_operator_authorization,
    validate_provider_endpoint,
)
from src.engine.market_data.normalizer import MarketDataNormalizer
from src.engine.market_data.provenance import compute_market_data_fingerprint

logger = logging.getLogger("tradepro.market_data_service")

# Curated catalog of standard market data instruments
SUPPORTED_INSTRUMENTS = [
    MarketDataInstrument(
        instrument_key="NSE_INDEX|Nifty 50",
        tradepro_instrument_id="NIFTY50_INDEX",
        name="Nifty 50 Index",
        exchange="NSE",
        segment="INDEX",
        lot_size=25,
        tick_size=Decimal("0.05"),
        supported_timeframes=["5m", "15m"],
    ),
    MarketDataInstrument(
        instrument_key="NSE_INDEX|Nifty Bank",
        tradepro_instrument_id="BANKNIFTY_INDEX",
        name="Nifty Bank Index",
        exchange="NSE",
        segment="INDEX",
        lot_size=15,
        tick_size=Decimal("0.05"),
        supported_timeframes=["5m", "15m"],
    ),
    MarketDataInstrument(
        instrument_key="NSE_EQ|INE002A01018",
        tradepro_instrument_id="RELIANCE_EQ",
        name="Reliance Industries Ltd",
        exchange="NSE",
        segment="EQ",
        lot_size=1,
        tick_size=Decimal("0.05"),
        supported_timeframes=["5m", "15m"],
    ),
    MarketDataInstrument(
        instrument_key="NSE_EQ|INE467B01029",
        tradepro_instrument_id="TCS_EQ",
        name="Tata Consultancy Services Ltd",
        exchange="NSE",
        segment="EQ",
        lot_size=1,
        tick_size=Decimal("0.05"),
        supported_timeframes=["5m", "15m"],
    ),
    MarketDataInstrument(
        instrument_key="NSE_EQ|INE009A01021",
        tradepro_instrument_id="INFY_EQ",
        name="Infosys Ltd",
        exchange="NSE",
        segment="EQ",
        lot_size=1,
        tick_size=Decimal("0.05"),
        supported_timeframes=["5m", "15m"],
    ),
]


class MarketDataService:
    """
    Coordinates read-only provider market data queries, readiness evaluation,
    and instrument catalog inspection.
    """

    @staticmethod
    def get_readiness(user_id: str) -> MarketDataReadinessResponse:
        """
        Evaluates market data readiness with strictly zero external network calls
        and zero database mutations.
        """
        raw_base_url = (os.environ.get("UPSTOX_MARKET_DATA_BASE_URL") or DEFAULT_UPSTOX_MARKET_DATA_BASE_URL).rstrip("/")
        is_endpoint_valid, sanitized_base_url, _ = validate_provider_endpoint(raw_base_url, APPROVED_MARKET_DATA_HOSTS)

        network_enabled = os.environ.get("UPSTOX_MARKET_DATA_ENABLED", "false").lower() in ("true", "1", "yes")
        has_token = bool(os.environ.get("UPSTOX_MARKET_DATA_ACCESS_TOKEN", "").strip())
        configured_owner = os.environ.get("UPSTOX_MARKET_DATA_OWNER_ID", "").strip()

        op_configured, is_authorized, _, _ = evaluate_operator_authorization(configured_owner, user_id)

        if not is_endpoint_valid:
            status = "INVALID_ENDPOINT_CONFIGURATION"
        elif not network_enabled:
            status = "NETWORK_DISABLED"
        elif not has_token:
            status = "CREDENTIALS_MISSING"
        elif not op_configured:
            status = "OPERATOR_NOT_CONFIGURED"
        elif not is_authorized:
            status = "FORBIDDEN_OPERATOR"
        else:
            status = "CONFIGURED_AND_ENABLED"

        return MarketDataReadinessResponse(
            network_enabled=network_enabled,
            credential_configured=has_token,
            operator_configured=op_configured,
            is_authorized_operator=is_authorized,
            status=status,
            base_url=sanitized_base_url,
            approved_hosts=APPROVED_MARKET_DATA_HOSTS,
            supported_timeframes=SUPPORTED_MARKET_DATA_TIMEFRAMES,
        )

    @staticmethod
    def get_supported_instruments() -> List[MarketDataInstrument]:
        return list(SUPPORTED_INSTRUMENTS)

    @staticmethod
    def fetch_and_normalize_candles(
        user_id: str,
        instrument_key: str,
        timeframe: str,
        mode: str,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        clock: Optional[Callable[[], datetime.datetime]] = None,
        adapter: Optional[UpstoxMarketDataAdapter] = None,
    ) -> MarketDataCandlesResponse:
        """
        Fetches and normalizes provider market data candles for an authorized operator.
        """
        # 1. Validation
        if timeframe not in SUPPORTED_MARKET_DATA_TIMEFRAMES:
            raise MarketDataValidationError(
                f"Unsupported timeframe '{timeframe}'. Supported: {SUPPORTED_MARKET_DATA_TIMEFRAMES}"
            )

        if mode not in ("intraday", "historical"):
            raise MarketDataValidationError(
                f"Invalid mode '{mode}'. Supported modes: 'intraday', 'historical'."
            )

        if not instrument_key or not isinstance(instrument_key, str):
            raise MarketDataValidationError("instrument_key must be a non-empty string.")

        # 2. Date validation for historical requests
        date_range_meta = None
        if mode == "historical":
            if not from_date or not to_date:
                raise MarketDataValidationError("from_date and to_date are required for historical mode.")

            try:
                d_from = datetime.date.fromisoformat(from_date)
                d_to = datetime.date.fromisoformat(to_date)
            except ValueError as e:
                raise MarketDataValidationError(f"Invalid date format: {str(e)}. Use YYYY-MM-DD.")

            if d_from > d_to:
                raise MarketDataValidationError(f"from_date ({from_date}) must be on or before to_date ({to_date}).")

            # Bounded request range: maximum 30 days
            delta_days = (d_to - d_from).days
            if delta_days > 30:
                raise MarketDataValidationError(
                    f"Requested historical range of {delta_days} days exceeds maximum allowed limit of 30 days."
                )

            date_range_meta = {"from_date": from_date, "to_date": to_date}

        # 3. Adapter instantiation and owner authorization
        active_adapter = adapter or UpstoxMarketDataAdapter()
        active_adapter.verify_operator_access(user_id)

        # 4. Fetch raw candles via bounded GET
        if mode == "intraday":
            raw_candles = active_adapter.get_intraday_candles(instrument_key, timeframe)
        else:
            raw_candles = active_adapter.get_historical_candles(instrument_key, timeframe, from_date, to_date)

        # 5. Normalization with clock injection and boundary filtering
        normalizer = MarketDataNormalizer(clock=clock)
        norm_result = normalizer.normalize_candles(
            raw_candles,
            timeframe,
            from_date=from_date if mode == "historical" else None,
            to_date=to_date if mode == "historical" else None,
            mode=mode,
        )

        # 6. Provenance and fingerprinting
        fingerprint = compute_market_data_fingerprint(norm_result.candles)
        retrieved_at = datetime.datetime.now(datetime.timezone.utc)

        provenance = MarketDataProvenance(
            provider="UPSTOX",
            source_type="PROVIDER_UPSTOX_V3",
            retrieved_at=retrieved_at,
            requested_instrument_key=instrument_key,
            timeframe=timeframe,
            mode=mode,
            date_range=date_range_meta,
            candle_count=len(norm_result.candles),
            content_fingerprint=fingerprint,
            completeness=norm_result.completeness,
            is_complete_series=norm_result.is_complete_series,
            warnings=norm_result.warnings,
        )

        # Find matching TradePro instrument identity if known
        tp_id = None
        for inst in SUPPORTED_INSTRUMENTS:
            if inst.instrument_key == instrument_key:
                tp_id = inst.tradepro_instrument_id
                break

        return MarketDataCandlesResponse(
            instrument_key=instrument_key,
            tradepro_instrument_id=tp_id,
            timeframe=timeframe,
            mode=mode,
            candles=norm_result.candles,
            provenance=provenance,
        )

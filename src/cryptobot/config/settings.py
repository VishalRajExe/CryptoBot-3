"""Configuration loading and validation.

Everything is validated eagerly and strictly: unknown keys are errors (so a typo
cannot silently disable a risk limit), and the Alpaca trading host must appear in
a paper-only allowlist.

There is intentionally no ``live`` mode, flag, or fallback anywhere in this file.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

import yaml

from cryptobot.core import timeframes
from cryptobot.core.contracts import OrderType, TimeInForce, normalize_symbol, to_decimal
from cryptobot.core.errors import ConfigError

#: The only trading hosts this application will ever talk to.
PAPER_TRADING_HOSTS = frozenset({"https://paper-api.alpaca.markets"})

#: Documented crypto data locations (Alpaca currently documents only ``us``).
CRYPTO_DATA_LOCATIONS = frozenset({"us"})

APP_ROOT = Path(__file__).resolve().parents[3]


# ---------------------------------------------------------------------------
# parsing helpers
# ---------------------------------------------------------------------------


def _mapping(value: Any, path: str) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ConfigError(f"{path}: expected a mapping, got {type(value).__name__}")
    return value


def _check_keys(raw: Mapping[str, Any], allowed: set[str], path: str) -> None:
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ConfigError(f"{path}: unknown key(s) {unknown}; allowed: {sorted(allowed)}")


def _dec(raw: Mapping[str, Any], key: str, path: str, default: str | None = None) -> Decimal:
    if key not in raw or raw[key] is None:
        if default is None:
            raise ConfigError(f"{path}.{key}: required")
        value: Any = default
    else:
        value = raw[key]
    try:
        return to_decimal(value, f"{path}.{key}")
    except Exception as exc:
        raise ConfigError(f"{path}.{key}: {exc}") from exc


def _int(raw: Mapping[str, Any], key: str, path: str, default: int | None = None) -> int:
    if key not in raw or raw[key] is None:
        if default is None:
            raise ConfigError(f"{path}.{key}: required")
        return default
    value = raw[key]
    if isinstance(value, bool) or not isinstance(value, int):
        if isinstance(value, str) and value.strip().isdigit():
            return int(value.strip())
        raise ConfigError(f"{path}.{key}: expected integer, got {value!r}")
    return value


def _float(raw: Mapping[str, Any], key: str, path: str, default: float) -> float:
    value = raw.get(key, default)
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{path}.{key}: expected number, got {value!r}") from exc


def _bool(raw: Mapping[str, Any], key: str, path: str, default: bool = False) -> bool:
    value = raw.get(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        if value.strip().lower() in {"true", "yes", "1", "on"}:
            return True
        if value.strip().lower() in {"false", "no", "0", "off"}:
            return False
    raise ConfigError(f"{path}.{key}: expected boolean, got {value!r}")


def _str(raw: Mapping[str, Any], key: str, path: str, default: str | None = None) -> str:
    if key not in raw or raw[key] is None:
        if default is None:
            raise ConfigError(f"{path}.{key}: required")
        return default
    value = raw[key]
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{path}.{key}: expected non-empty string")
    return value.strip()


def _str_list(raw: Mapping[str, Any], key: str, path: str) -> tuple[str, ...]:
    value = raw.get(key)
    if value is None:
        raise ConfigError(f"{path}.{key}: required")
    if not isinstance(value, (list, tuple)) or not value:
        raise ConfigError(f"{path}.{key}: expected a non-empty list")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ConfigError(f"{path}.{key}: entries must be non-empty strings")
        out.append(item.strip())
    return tuple(out)


# ---------------------------------------------------------------------------
# sections
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AppConfig:
    name: str
    environment: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "AppConfig":
        _check_keys(raw, {"name", "environment"}, "app")
        environment = _str(raw, "environment", "app", "paper")
        if environment != "paper":
            raise ConfigError(
                f"app.environment={environment!r} refused. This project is paper-only "
                "by construction and has no live-trading mode."
            )
        return cls(name=_str(raw, "name", "app", "cryptobot"), environment=environment)


@dataclass(frozen=True, slots=True)
class AllocationConfig:
    ceiling_usd: Decimal

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "AllocationConfig":
        _check_keys(raw, {"ceiling_usd"}, "allocation")
        ceiling = _dec(raw, "ceiling_usd", "allocation")
        if ceiling <= 0:
            raise ConfigError("allocation.ceiling_usd must be > 0")
        return cls(ceiling_usd=ceiling)


@dataclass(frozen=True, slots=True)
class AlpacaConfig:
    trading_base_url: str
    data_base_url: str
    crypto_location: str
    api_key_env: str
    api_secret_env: str
    request_timeout_seconds: float
    max_retries: int

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "AlpacaConfig":
        _check_keys(
            raw,
            {
                "trading_base_url",
                "data_base_url",
                "crypto_location",
                "api_key_env",
                "api_secret_env",
                "request_timeout_seconds",
                "max_retries",
            },
            "alpaca",
        )
        trading_url = _str(raw, "trading_base_url", "alpaca").rstrip("/")
        if trading_url not in PAPER_TRADING_HOSTS:
            raise ConfigError(
                f"alpaca.trading_base_url={trading_url!r} is not a paper endpoint. "
                f"Allowed: {sorted(PAPER_TRADING_HOSTS)}"
            )
        data_url = _str(raw, "data_base_url", "alpaca").rstrip("/")
        if not data_url.startswith("https://"):
            raise ConfigError("alpaca.data_base_url must use https")
        location = _str(raw, "crypto_location", "alpaca", "us")
        if location not in CRYPTO_DATA_LOCATIONS:
            raise ConfigError(
                f"alpaca.crypto_location={location!r} unsupported; documented values: "
                f"{sorted(CRYPTO_DATA_LOCATIONS)}"
            )
        return cls(
            trading_base_url=trading_url,
            data_base_url=data_url,
            crypto_location=location,
            api_key_env=_str(raw, "api_key_env", "alpaca", "ALPACA_API_KEY"),
            api_secret_env=_str(raw, "api_secret_env", "alpaca", "ALPACA_API_SECRET_KEY"),
            request_timeout_seconds=_float(raw, "request_timeout_seconds", "alpaca", 20.0),
            max_retries=_int(raw, "max_retries", "alpaca", 3),
        )

    def missing_credential_names(self, env: Mapping[str, str] | None = None) -> list[str]:
        env = os.environ if env is None else env
        return [n for n in (self.api_key_env, self.api_secret_env) if not env.get(n)]

    def credentials(self, env: Mapping[str, str] | None = None) -> tuple[str, str]:
        env = os.environ if env is None else env
        missing = self.missing_credential_names(env)
        if missing:
            raise ConfigError(
                "missing broker credentials in environment: "
                + ", ".join(missing)
                + ". Set them in your shell (see .env.example). Never commit them."
            )
        return env[self.api_key_env], env[self.api_secret_env]

    @property
    def bars_path(self) -> str:
        return f"/v1beta3/crypto/{self.crypto_location}/bars"

    @property
    def assets_path(self) -> str:
        return "/v2/assets"


@dataclass(frozen=True, slots=True)
class MarketConfig:
    symbols: tuple[str, ...]
    timeframe: str
    max_bar_age_seconds: int
    lookback_bars: int

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "MarketConfig":
        _check_keys(raw, {"symbols", "timeframe", "max_bar_age_seconds", "lookback_bars"}, "market")
        try:
            symbols = tuple(normalize_symbol(s) for s in _str_list(raw, "symbols", "market"))
        except Exception as exc:
            raise ConfigError(f"market.symbols: {exc}") from exc
        try:
            timeframe = timeframes.canonical(_str(raw, "timeframe", "market"))
        except Exception as exc:
            raise ConfigError(f"market.timeframe: {exc}") from exc
        max_age = _int(raw, "max_bar_age_seconds", "market", 7200)
        if max_age <= 0:
            raise ConfigError("market.max_bar_age_seconds must be > 0")
        lookback = _int(raw, "lookback_bars", "market", 200)
        if lookback < 2:
            raise ConfigError("market.lookback_bars must be >= 2")
        return cls(symbols, timeframe, max_age, lookback)


@dataclass(frozen=True, slots=True)
class StrategyConfig:
    name: str
    params: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "StrategyConfig":
        _check_keys(raw, {"name", "params"}, "strategy")
        params = _mapping(raw.get("params"), "strategy.params")
        return cls(name=_str(raw, "name", "strategy"), params=dict(params))

    def int_param(self, key: str, default: int | None = None) -> int:
        if key not in self.params:
            if default is None:
                raise ConfigError(f"strategy.params.{key}: required by strategy {self.name!r}")
            return default
        value = self.params[key]
        if isinstance(value, bool):
            raise ConfigError(f"strategy.params.{key}: expected integer")
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"strategy.params.{key}: expected integer") from exc

    def decimal_param(self, key: str, default: Decimal | None = None) -> Decimal:
        if key not in self.params:
            if default is None:
                raise ConfigError(f"strategy.params.{key}: required by strategy {self.name!r}")
            return default
        try:
            return to_decimal(self.params[key], f"strategy.params.{key}")
        except Exception as exc:
            raise ConfigError(str(exc)) from exc


@dataclass(frozen=True, slots=True)
class RiskConfig:
    max_position_pct: Decimal
    max_total_exposure_pct: Decimal
    per_trade_risk_pct: Decimal
    daily_loss_limit_pct: Decimal
    max_drawdown_pct: Decimal
    fee_reserve_bps: Decimal
    slippage_reserve_bps: Decimal
    max_orders_per_day: int
    max_spread_bps: Decimal
    symbol_allowlist: tuple[str, ...]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "RiskConfig":
        _check_keys(
            raw,
            {
                "max_position_pct",
                "max_total_exposure_pct",
                "per_trade_risk_pct",
                "daily_loss_limit_pct",
                "max_drawdown_pct",
                "fee_reserve_bps",
                "slippage_reserve_bps",
                "max_orders_per_day",
                "max_spread_bps",
                "symbol_allowlist",
            },
            "risk",
        )
        try:
            allowlist = tuple(
                normalize_symbol(s) for s in _str_list(raw, "symbol_allowlist", "risk")
            )
        except Exception as exc:
            raise ConfigError(f"risk.symbol_allowlist: {exc}") from exc

        cfg = cls(
            max_position_pct=_dec(raw, "max_position_pct", "risk"),
            max_total_exposure_pct=_dec(raw, "max_total_exposure_pct", "risk"),
            per_trade_risk_pct=_dec(raw, "per_trade_risk_pct", "risk"),
            daily_loss_limit_pct=_dec(raw, "daily_loss_limit_pct", "risk"),
            max_drawdown_pct=_dec(raw, "max_drawdown_pct", "risk"),
            fee_reserve_bps=_dec(raw, "fee_reserve_bps", "risk", "25"),
            slippage_reserve_bps=_dec(raw, "slippage_reserve_bps", "risk", "25"),
            max_orders_per_day=_int(raw, "max_orders_per_day", "risk", 6),
            max_spread_bps=_dec(raw, "max_spread_bps", "risk", "50"),
            symbol_allowlist=allowlist,
        )
        if not (Decimal("0") < cfg.max_position_pct <= Decimal("100")):
            raise ConfigError("risk.max_position_pct must be in (0, 100]")
        if not (Decimal("0") < cfg.max_total_exposure_pct <= Decimal("100")):
            raise ConfigError("risk.max_total_exposure_pct must be in (0, 100]")
        if not (Decimal("0") < cfg.per_trade_risk_pct <= Decimal("100")):
            raise ConfigError("risk.per_trade_risk_pct must be in (0, 100]")
        if cfg.per_trade_risk_pct > cfg.max_total_exposure_pct:
            raise ConfigError(
                "risk.per_trade_risk_pct must not exceed risk.max_total_exposure_pct"
            )
        if not (Decimal("0") < cfg.daily_loss_limit_pct <= Decimal("100")):
            raise ConfigError("risk.daily_loss_limit_pct must be in (0, 100]")
        if not (Decimal("0") < cfg.max_drawdown_pct <= Decimal("100")):
            raise ConfigError("risk.max_drawdown_pct must be in (0, 100]")
        if cfg.fee_reserve_bps < 0 or cfg.slippage_reserve_bps < 0:
            raise ConfigError("risk fee/slippage reserves must be >= 0")
        if cfg.max_orders_per_day < 1:
            raise ConfigError("risk.max_orders_per_day must be >= 1")
        return cfg

    @property
    def fee_reserve_fraction(self) -> Decimal:
        return self.fee_reserve_bps / Decimal("10000")

    @property
    def slippage_reserve_fraction(self) -> Decimal:
        return self.slippage_reserve_bps / Decimal("10000")


@dataclass(frozen=True, slots=True)
class ExecutionConfig:
    order_type: OrderType
    time_in_force: TimeInForce
    max_retries: int
    retry_backoff_seconds: float
    lost_order_grace_seconds: int

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ExecutionConfig":
        _check_keys(
            raw,
            {
                "order_type",
                "time_in_force",
                "max_retries",
                "retry_backoff_seconds",
                "lost_order_grace_seconds",
            },
            "execution",
        )
        raw_type = _str(raw, "order_type", "execution", "market").lower()
        try:
            order_type = OrderType(raw_type)
        except ValueError as exc:
            raise ConfigError(
                f"execution.order_type={raw_type!r} unsupported; crypto supports "
                f"{[t.value for t in OrderType]}"
            ) from exc

        raw_tif = _str(raw, "time_in_force", "execution", "gtc").lower()
        if raw_tif == "day":
            raise ConfigError(
                "execution.time_in_force='day' is a stock-only value and is invalid for "
                "Alpaca crypto orders; use 'gtc' or 'ioc' (see docs/API_CAPABILITIES.md)"
            )
        try:
            tif = TimeInForce(raw_tif)
        except ValueError as exc:
            raise ConfigError(
                f"execution.time_in_force={raw_tif!r} unsupported for crypto; "
                f"allowed: {[t.value for t in TimeInForce]}"
            ) from exc

        max_retries = _int(raw, "max_retries", "execution", 3)
        if not 0 <= max_retries <= 5:
            raise ConfigError("execution.max_retries must be in [0, 5]")
        backoff = _float(raw, "retry_backoff_seconds", "execution", 1.5)
        if backoff < 0:
            raise ConfigError("execution.retry_backoff_seconds must be >= 0")
        grace = _int(raw, "lost_order_grace_seconds", "execution", 120)
        if grace < 0:
            raise ConfigError("execution.lost_order_grace_seconds must be >= 0")
        return cls(order_type, tif, max_retries, backoff, grace)


@dataclass(frozen=True, slots=True)
class JournalConfig:
    path: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "JournalConfig":
        _check_keys(raw, {"path"}, "journal")
        return cls(path=_str(raw, "path", "journal", "var/cryptobot.db"))


@dataclass(frozen=True, slots=True)
class MonitoringConfig:
    dashboard_enabled: bool
    dashboard_host: str
    dashboard_port: int
    telegram_enabled: bool

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "MonitoringConfig":
        _check_keys(
            raw,
            {"dashboard_enabled", "dashboard_host", "dashboard_port", "telegram_enabled"},
            "monitoring",
        )
        port = _int(raw, "dashboard_port", "monitoring", 8787)
        if not 1024 <= port <= 65535:
            raise ConfigError("monitoring.dashboard_port must be in [1024, 65535]")
        host = _str(raw, "dashboard_host", "monitoring", "127.0.0.1")
        if host not in {"127.0.0.1", "localhost"}:
            raise ConfigError(
                "monitoring.dashboard_host must bind to loopback (127.0.0.1 or localhost); "
                "the dashboard displays account state"
            )
        return cls(
            dashboard_enabled=_bool(raw, "dashboard_enabled", "monitoring", False),
            dashboard_host=host,
            dashboard_port=port,
            telegram_enabled=_bool(raw, "telegram_enabled", "monitoring", False),
        )


# ---------------------------------------------------------------------------
# root settings
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Settings:
    app: AppConfig
    allocation: AllocationConfig
    alpaca: AlpacaConfig
    market: MarketConfig
    strategy: StrategyConfig
    risk: RiskConfig
    execution: ExecutionConfig
    journal: JournalConfig
    monitoring: MonitoringConfig
    source_path: str | None = None

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any], source_path: str | None = None) -> "Settings":
        if not isinstance(raw, Mapping):
            raise ConfigError("configuration root must be a mapping")
        _check_keys(
            raw,
            {
                "app",
                "allocation",
                "alpaca",
                "market",
                "strategy",
                "risk",
                "execution",
                "journal",
                "monitoring",
            },
            "<root>",
        )
        settings = cls(
            app=AppConfig.from_dict(_mapping(raw.get("app"), "app")),
            allocation=AllocationConfig.from_dict(_mapping(raw.get("allocation"), "allocation")),
            alpaca=AlpacaConfig.from_dict(_mapping(raw.get("alpaca"), "alpaca")),
            market=MarketConfig.from_dict(_mapping(raw.get("market"), "market")),
            strategy=StrategyConfig.from_dict(_mapping(raw.get("strategy"), "strategy")),
            risk=RiskConfig.from_dict(_mapping(raw.get("risk"), "risk")),
            execution=ExecutionConfig.from_dict(_mapping(raw.get("execution"), "execution")),
            journal=JournalConfig.from_dict(_mapping(raw.get("journal"), "journal")),
            monitoring=MonitoringConfig.from_dict(_mapping(raw.get("monitoring"), "monitoring")),
            source_path=source_path,
        )
        settings.validate()
        return settings

    @classmethod
    def from_file(cls, path: str | Path) -> "Settings":
        target = Path(path)
        if not target.is_file():
            raise ConfigError(f"configuration file not found: {target}")
        try:
            raw = yaml.safe_load(target.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise ConfigError(f"{target}: invalid YAML: {exc}") from exc
        if raw is None:
            raise ConfigError(f"{target}: configuration is empty")
        return cls.from_dict(raw, source_path=str(target))

    def validate(self) -> None:
        missing = [s for s in self.market.symbols if s not in self.risk.symbol_allowlist]
        if missing:
            raise ConfigError(
                f"market.symbols {missing} are not in risk.symbol_allowlist "
                f"{list(self.risk.symbol_allowlist)}"
            )
        if not self.strategy.name:
            raise ConfigError("strategy.name must not be empty")
        timeframes.canonical(self.market.timeframe)  # raises for unsupported values

    # -- derived paths ------------------------------------------------------

    def database_path(self, env: Mapping[str, str] | None = None) -> Path:
        env = os.environ if env is None else env
        override = env.get("CRYPTOBOT_DB")
        raw = override or self.journal.path
        path = Path(raw)
        return path if path.is_absolute() else (APP_ROOT / path)

    # -- safe display -------------------------------------------------------

    def as_public_dict(self, env: Mapping[str, str] | None = None) -> dict[str, Any]:
        """Configuration for display. Never contains secret values."""
        env = os.environ if env is None else env
        return {
            "app": {"name": self.app.name, "environment": self.app.environment},
            "allocation": {"ceiling_usd": format(self.allocation.ceiling_usd, "f")},
            "alpaca": {
                "trading_base_url": self.alpaca.trading_base_url,
                "data_base_url": self.alpaca.data_base_url,
                "crypto_location": self.alpaca.crypto_location,
                "bars_path": self.alpaca.bars_path,
                "api_key_env": self.alpaca.api_key_env,
                "api_secret_env": self.alpaca.api_secret_env,
                "credentials_present": not self.alpaca.missing_credential_names(env),
                "request_timeout_seconds": self.alpaca.request_timeout_seconds,
            },
            "market": {
                "symbols": list(self.market.symbols),
                "timeframe": self.market.timeframe,
                "max_bar_age_seconds": self.market.max_bar_age_seconds,
                "lookback_bars": self.market.lookback_bars,
            },
            "strategy": {"name": self.strategy.name, "params": dict(self.strategy.params)},
            "risk": {
                "max_position_pct": format(self.risk.max_position_pct, "f"),
                "max_total_exposure_pct": format(self.risk.max_total_exposure_pct, "f"),
                "per_trade_risk_pct": format(self.risk.per_trade_risk_pct, "f"),
                "daily_loss_limit_pct": format(self.risk.daily_loss_limit_pct, "f"),
                "max_drawdown_pct": format(self.risk.max_drawdown_pct, "f"),
                "symbol_allowlist": list(self.risk.symbol_allowlist),
            },
            "execution": {
                "order_type": self.execution.order_type.value,
                "time_in_force": self.execution.time_in_force.value,
                "lost_order_grace_seconds": self.execution.lost_order_grace_seconds,
            },
            "journal": {"path": str(self.database_path(env))},
            "monitoring": {
                "dashboard_enabled": self.monitoring.dashboard_enabled,
                "dashboard_host": self.monitoring.dashboard_host,
                "dashboard_port": self.monitoring.dashboard_port,
                "telegram_enabled": self.monitoring.telegram_enabled,
            },
            "source_path": self.source_path,
        }


def default_config_path() -> Path:
    return APP_ROOT / "config" / "trading.yaml"


def load_settings(path: str | Path | None = None) -> Settings:
    return Settings.from_file(path or default_config_path())

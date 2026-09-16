BEGIN;

CREATE SCHEMA IF NOT EXISTS v2;

CREATE TABLE IF NOT EXISTS v2.runtime_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS v2.positions (
    symbol TEXT PRIMARY KEY,
    entry_price NUMERIC(38,18) NOT NULL CHECK (entry_price > 0),
    quantity NUMERIC(38,18) NOT NULL CHECK (quantity > 0),
    quote_size NUMERIC(38,18) NOT NULL CHECK (quote_size > 0),
    take_profit NUMERIC(38,18) NOT NULL CHECK (take_profit > 0),
    stop_loss NUMERIC(38,18) NOT NULL CHECK (stop_loss > 0),
    opened_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS v2.trades (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    symbol TEXT NOT NULL,
    entry_price NUMERIC(38,18) NOT NULL CHECK (entry_price > 0),
    exit_price NUMERIC(38,18) NOT NULL CHECK (exit_price > 0),
    quantity NUMERIC(38,18) NOT NULL CHECK (quantity > 0),
    pnl_usdt NUMERIC(38,18) NOT NULL,
    reason TEXT NOT NULL CHECK (length(reason) > 0),
    opened_at TIMESTAMPTZ NOT NULL,
    closed_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_v2_trades_closed_at ON v2.trades (closed_at);

CREATE TABLE IF NOT EXISTS v2.emitted_signals (
    symbol TEXT NOT NULL,
    signal_open_time DOUBLE PRECISION NOT NULL CHECK (signal_open_time > 0),
    kind TEXT NOT NULL CHECK (length(kind) > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (symbol, signal_open_time, kind)
);

CREATE TABLE IF NOT EXISTS v2.execution_journal (
    client_order_id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    stage TEXT NOT NULL CHECK (
        stage IN (
            'INTENT','BUY_UNKNOWN','BUY_FILLED','OCO_INTENT','OCO_UNKNOWN',
            'UNPROTECTED','PROTECTED','FLATTENED','ABORTED'
        )
    ),
    details_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_v2_execution_journal_stage_created
    ON v2.execution_journal (stage, created_at);

CREATE TABLE IF NOT EXISTS v2.shadow_outcomes (
    symbol TEXT NOT NULL,
    signal_open_time DOUBLE PRECISION NOT NULL CHECK (signal_open_time > 0),
    entry_price NUMERIC(38,18) NOT NULL CHECK (entry_price > 0),
    quantity NUMERIC(38,18) NOT NULL CHECK (quantity > 0),
    take_profit NUMERIC(38,18) NOT NULL CHECK (take_profit > 0),
    stop_loss NUMERIC(38,18) NOT NULL CHECK (stop_loss > 0),
    fee_rate NUMERIC(18,12) NOT NULL CHECK (fee_rate >= 0 AND fee_rate < 0.02),
    status TEXT NOT NULL CHECK (status IN ('OPEN','TP','SL','AMBIGUOUS')),
    exit_price NUMERIC(38,18),
    pnl_usdt NUMERIC(38,18),
    reason TEXT,
    opened_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    closed_at TIMESTAMPTZ,
    PRIMARY KEY (symbol, signal_open_time),
    CHECK ((status = 'OPEN' AND closed_at IS NULL) OR (status <> 'OPEN' AND closed_at IS NOT NULL)),
    CHECK ((status = 'AMBIGUOUS' AND exit_price IS NULL AND pnl_usdt IS NULL) OR status <> 'AMBIGUOUS')
);
CREATE INDEX IF NOT EXISTS idx_v2_shadow_outcomes_status_opened
    ON v2.shadow_outcomes (status, opened_at);

COMMIT;

"""Tests for symbol normalization."""

from apex_common.symbols import normalize_symbols


def test_compact_btcusdt():
    raw, binance, ccxt = normalize_symbols("BTCUSDT")
    assert binance == "BTCUSDT"
    assert ccxt == "BTC/USDT:USDT"


def test_ccxt_slash():
    raw, binance, ccxt = normalize_symbols("BTC/USDT")
    assert binance == "BTCUSDT"
    assert ccxt == "BTC/USDT:USDT"


def test_ccxt_swap():
    raw, binance, ccxt = normalize_symbols("BTC/USDT:USDT")
    assert binance == "BTCUSDT"
    assert ccxt == "BTC/USDT:USDT"


def test_lowercase():
    raw, binance, ccxt = normalize_symbols("btcusdt")
    assert binance == "BTCUSDT"
    assert ccxt == "BTC/USDT:USDT"


def test_ethusdt():
    raw, binance, ccxt = normalize_symbols("ETHUSDT")
    assert binance == "ETHUSDT"
    assert ccxt == "ETH/USDT:USDT"


def test_solusdt():
    raw, binance, ccxt = normalize_symbols("SOL/USDT:USDT")
    assert binance == "SOLUSDT"
    assert ccxt == "SOL/USDT:USDT"


def test_dash_separator():
    raw, binance, ccxt = normalize_symbols("BTC-USDT")
    assert binance == "BTCUSDT"
    assert ccxt == "BTC/USDT:USDT"

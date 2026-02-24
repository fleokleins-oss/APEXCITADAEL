"""Symbol normalization for multi-exchange support.

Accepts: BTCUSDT, BTC/USDT, BTC/USDT:USDT, BTC-USDT
Produces: (raw_input, binance_symbol, ccxt_swap_symbol)
"""

from __future__ import annotations

COMMON_QUOTES = ("USDT", "USDC", "BUSD", "USD", "BTC", "ETH")


def _parse_ccxt_symbol(sym: str) -> tuple[str, str] | None:
    s = sym.strip().upper()
    if "/" in s:
        base = s.split("/")[0].strip()
        rest = s.split("/")[1].strip()
        quote = rest.split(":")[0].strip() if ":" in rest else rest
        if base and quote:
            return base, quote
    return None


def _parse_compact_symbol(sym: str) -> tuple[str, str] | None:
    s = sym.strip().upper().replace("-", "").replace("_", "")
    for q in sorted(COMMON_QUOTES, key=len, reverse=True):
        if s.endswith(q) and len(s) > len(q):
            return s[: -len(q)], q
    return None


def normalize_symbols(sym: str) -> tuple[str, str, str]:
    """Return (raw_input, binance_compact, ccxt_swap).

    Examples:
        "BTCUSDT"       → ("BTCUSDT", "BTCUSDT", "BTC/USDT:USDT")
        "BTC/USDT"      → ("BTC/USDT", "BTCUSDT", "BTC/USDT:USDT")
        "BTC/USDT:USDT" → ("BTC/USDT:USDT", "BTCUSDT", "BTC/USDT:USDT")
    """
    s_in = sym.strip()
    parsed = _parse_ccxt_symbol(s_in) or _parse_compact_symbol(s_in)
    if parsed:
        base, quote = parsed
        return s_in, f"{base}{quote}", f"{base}/{quote}:{quote}"
    return s_in, s_in.strip().upper(), s_in.strip()

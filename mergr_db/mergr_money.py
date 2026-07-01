"""Shared money/multiple formatting — used by BOTH the Streamlit app and the API
so display strings never diverge. Every money field returns raw + formatted."""

CUR_SYM = {"USD": "$", "EUR": "€", "GBP": "£", "JPY": "¥", "CNY": "¥", "INR": "₹",
           "AUD": "A$", "CAD": "C$", "NZD": "NZ$", "HKD": "HK$", "KRW": "₩", "BRL": "R$"}
SCALE_ABBR = {"thousands": "K", "millions": "M", "billions": "B"}


def _num(amount):
    if amount is None:
        return None
    try:
        return float(str(amount).replace(",", ""))
    except (TypeError, ValueError):
        return None


def money_str(amount, currency=None, scale="millions"):
    n = _num(amount)
    if n is None:
        return "—"
    ab = SCALE_ABBR.get(scale or "millions", "M")
    amt = f"{n:,.0f}"
    cur = (currency or "").strip()
    sym = CUR_SYM.get(cur)
    return f"{sym}{amt}{ab}" if sym else f"{amt}{ab} {cur}".strip()


def mult_str(x):
    n = _num(x)
    return f"{n:.1f}×" if n is not None else "—"


def money_obj(amount, currency=None, scale="millions", usd_rate=None):
    """Full money field for the API: raw amount + currency + scale + formatted
    string + USD-normalised amount (if a rate is supplied). None if no amount."""
    n = _num(amount)
    if n is None:
        return None
    return {
        "amount": n,
        "currency": currency,
        "scale": scale or "millions",
        "formatted": money_str(n, currency, scale),
        "usd": round(n * usd_rate, 2) if usd_rate is not None else None,
    }


def mult_obj(x):
    n = _num(x)
    return None if n is None else {"value": n, "formatted": mult_str(n)}

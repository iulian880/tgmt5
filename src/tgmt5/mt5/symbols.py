"""Resolve canonical symbols to the names actually offered by the broker."""
from __future__ import annotations

import re

from tgmt5.mt5.interface import MT5Gateway
from tgmt5.parsing.symbols import CURRENCIES, broker_candidates, canonicalize

_SUFFIX_RE = re.compile(r"^[._\-+#]?[A-Za-z0-9]{0,4}$")


def _is_valid_suffix(suffix: str) -> bool:
    if not suffix or not _SUFFIX_RE.match(suffix):
        return False
    # Never treat a trailing currency code without a separator (e.g. GOLD + EUR = GOLDEUR) as a broker suffix.
    if suffix.upper() in CURRENCIES:
        return False
    return True


class SymbolResolver:
    def __init__(self, gateway: MT5Gateway, suffix: str = "") -> None:
        self.gw = gateway
        self.suffix = suffix
        self._cache: dict[str, str | None] = {}
        self._reverse: dict[str, str] = {}
        self._universe: list[str] | None = None

    def refresh(self) -> None:
        self._universe = self.gw.symbols()
        self._cache.clear()

    @property
    def universe(self) -> list[str]:
        if self._universe is None:
            self.refresh()
        return self._universe or []

    def resolve(self, canonical: str) -> str | None:
        """Return the broker symbol, or None if missing/ambiguous (never guesses)."""
        if canonical in self._cache:
            return self._cache[canonical]
        names = set(self.universe)
        names_ci = {n.upper(): n for n in self.universe}
        result: str | None = None
        for cand in broker_candidates(canonical):
            if self.suffix:
                want = cand + self.suffix
                if want in names:
                    result = want
                    break
                if want.upper() in names_ci:
                    result = names_ci[want.upper()]
                    break
            if cand in names:
                result = cand
                break
            if cand.upper() in names_ci:
                result = names_ci[cand.upper()]
                break
            suffixed = [n for n in names if n.upper().startswith(cand.upper())
                        and _is_valid_suffix(n[len(cand):])]
            if len(suffixed) == 1:
                result = suffixed[0]
                break
            if len(suffixed) > 1:
                result = None    # ambiguous: several variants (e.g. .a and .r) - require config
                break
        if result is not None:
            self.gw.symbol_select(result)
            self._reverse[result] = canonical
        self._cache[canonical] = result
        return result

    def canonical_of(self, broker_symbol: str) -> str:
        if broker_symbol in self._reverse:
            return self._reverse[broker_symbol]
        base = broker_symbol
        if self.suffix and base.endswith(self.suffix):
            base = base[: -len(self.suffix)]
        c = canonicalize(base) or canonicalize(re.sub(r"[._\-+#].*$", "", base))
        return c or broker_symbol

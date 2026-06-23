from __future__ import annotations

try:
    from tqdm.auto import tqdm as _tqdm
except ImportError:  # pragma: no cover - fallback only used if tqdm is absent
    class _NoOpTqdm:
        def __init__(
            self,
            iterable=None,
            total: int | None = None,
            desc: str | None = None,
            unit: str | None = None,
            leave: bool = True,
            dynamic_ncols: bool = True,
            **kwargs,
        ):
            self.iterable = iterable
            self.total = total
            self.desc = desc or ""
            self.unit = unit or "it"
            self.n = 0

        def __iter__(self):
            if self.iterable is None:
                return iter(())
            for item in self.iterable:
                self.n += 1
                yield item

        def update(self, n: int = 1):
            self.n += n

        def set_description_str(self, desc: str):
            self.desc = desc

        def set_postfix_str(self, text: str):
            return None

        def close(self):
            return None

    def tqdm(*args, **kwargs):
        return _NoOpTqdm(*args, **kwargs)
else:
    def tqdm(*args, **kwargs):
        kwargs.setdefault("dynamic_ncols", True)
        return _tqdm(*args, **kwargs)

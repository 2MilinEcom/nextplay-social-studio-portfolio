"""Minimal LooseVersion used by undetected_chromedriver on Python 3.14."""


class LooseVersion:
    def __init__(self, vstring=None):
        self.vstring = "" if vstring is None else str(vstring).strip()
        parts = []
        for part in self.vstring.split("."):
            try:
                parts.append(int(part))
            except ValueError:
                parts.append(part)
        self.version = tuple(parts)

    def __str__(self):
        return self.vstring

    def __repr__(self):
        return f"LooseVersion({self.vstring!r})"

    def _cmp_key(self, other):
        if isinstance(other, LooseVersion):
            return other.version
        return LooseVersion(other).version

    def __eq__(self, other):
        return self.version == self._cmp_key(other)

    def __lt__(self, other):
        return self.version < self._cmp_key(other)

    def __le__(self, other):
        return self.version <= self._cmp_key(other)

    def __gt__(self, other):
        return self.version > self._cmp_key(other)

    def __ge__(self, other):
        return self.version >= self._cmp_key(other)

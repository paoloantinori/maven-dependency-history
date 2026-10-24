"""Hard resource limits applied before any parsing work starts."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Limits:
    """Immutable input-size caps for untrusted artifacts."""

    xml_bytes: int = 8 * 1024 * 1024

    def __post_init__(self) -> None:
        if isinstance(self.xml_bytes, bool) or not isinstance(self.xml_bytes, int):
            raise TypeError(f"xml_bytes must be int, got {type(self.xml_bytes).__name__}")
        if self.xml_bytes <= 0:
            raise ValueError(f"xml_bytes must be positive, got {self.xml_bytes}")

"""Xteink device profiles."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Device:
    key: str
    name: str
    width: int
    height: int
    inches: float
    gray_levels: int = 4

    @property
    def label(self) -> str:
        return f"{self.width}×{self.height} • {self.inches}″"


DEVICES = {
    "x4": Device("x4", "X4", 480, 800, 4.3),
    "x3": Device("x3", "X3", 528, 792, 3.7),
}


def get_device(key: str) -> Device:
    return DEVICES.get((key or "x4").lower(), DEVICES["x4"])

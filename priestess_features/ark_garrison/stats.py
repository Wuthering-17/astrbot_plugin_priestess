"""Garrison leaderboard rows, adapted from Billtranscend/Priestess-bot (MIT)."""

from dataclasses import dataclass


@dataclass
class Standing:
    owner: str
    uid: str
    name: str
    trophies: int
    total: int
    wins: int
    band: str

    @property
    def rate(self) -> int:
        return round(100 * self.wins / self.total) if self.total else 0


def ranked(rows: list[Standing]) -> list[Standing]:
    return sorted(rows, key=lambda row: (-row.trophies, -row.wins, row.total, row.name, row.owner))

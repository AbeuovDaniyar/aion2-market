"""The source-agnostic data interface. Analysis and UI code depend on this, not on a site."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Generic, Literal, TypeVar

from app.external import models as m

T = TypeVar("T")

HealthState = Literal["ok", "stale", "failing", "down"]


class SourceError(Exception):
    """The source could not answer and nothing usable is cached."""


@dataclass(frozen=True)
class Fetched(Generic[T]):
    data: T
    fetched_at: datetime  # when this body came off the network
    from_cache: bool = False
    stale: bool = False  # served from cache because the live call failed


@dataclass(frozen=True)
class Freshness:
    """What the status bar shows and what gates alerts."""

    state: HealthState  # ok = green, stale = amber, failing/down = red
    checked_at: datetime
    snapshot_at: datetime | None = None  # last listings snapshot
    snapshot_age_seconds: int | None = None
    sales_batch_at: datetime | None = None  # last sales batch day
    sales_stale: bool = False
    failed_runs_since_ok: int = 0
    detail: str = ""

    @property
    def alerts_paused(self) -> bool:
        return self.state != "ok"


class PriceSource(ABC):
    name: str

    @abstractmethod
    def servers(self) -> Fetched[m.ServerList]: ...

    @abstractmethod
    def health(self, server: int | None = None) -> Fetched[m.Health]: ...

    @abstractmethod
    def collection(self, server: int | None = None) -> Fetched[m.Collection]: ...

    @abstractmethod
    def categories(self, server: int | None = None) -> Fetched[m.Categories]: ...

    @abstractmethod
    def overview(self, server: int | None = None, window: str = "24h") -> Fetched[m.Overview]: ...

    @abstractmethod
    def all_items(self, server: int | None = None) -> Fetched[list[m.ItemSummary]]: ...

    @abstractmethod
    def items_by_keys(
        self, keys: list[int], server: int | None = None
    ) -> Fetched[list[m.ItemSummary]]: ...

    @abstractmethod
    def prices(
        self, item_key: int, server: int | None = None, bucket: str | None = None
    ) -> Fetched[m.PriceSeries]: ...

    @abstractmethod
    def depth(
        self, item_key: int, units: int = 10, server: int | None = None
    ) -> Fetched[m.DepthSeries]: ...

    @abstractmethod
    def depth_ladder(self, item_key: int, server: int | None = None) -> Fetched[m.DepthLadder]: ...

    @abstractmethod
    def volume(self, item_key: int, server: int | None = None) -> Fetched[m.VolumeSeries]: ...

    @abstractmethod
    def observations(
        self, item_key: int, server: int | None = None
    ) -> Fetched[m.ObservationSeries]: ...

    @abstractmethod
    def crafts(self, server: int | None = None) -> Fetched[m.CraftList]: ...

    @abstractmethod
    def craft(self, target_key: int, server: int | None = None) -> Fetched[m.CraftDetail]: ...

    @abstractmethod
    def crafts_model(self, server: int | None = None) -> Fetched[m.CraftModelInfo]: ...

    @abstractmethod
    def freshness(self, server: int | None = None) -> Freshness: ...

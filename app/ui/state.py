"""What the tabs show, computed from SQLite on the UI thread. No network here."""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QObject, Signal

from app.analysis import alerts, flips, service
from app.analysis.fees import Fees
from app.db.repo import Database, icons_dir, utcnow
from app.external import models as m
from app.external.aion2exchange import read_cached
from app.external.scheduler import RefreshResult
from app.external.source import Freshness
from app.external.icons import icon_files
from app.ui.icons import icon_for, set_icon_files


@dataclass(frozen=True)
class Opportunity:
    kind: str  # 'Flip' | 'Craft' | 'Cheap vs region' | 'Mover'
    item_key: int
    name: str
    detail: str
    profit: float | None  # expected profit within the horizon, where one can be put on it
    confidence: str

    @property
    def id(self) -> str:
        return f"{self.kind}:{self.item_key}"


class AppState(QObject):
    changed = Signal()
    alerts_raised = Signal(list)  # new alerts since the last reload; not emitted while data is stale

    def __init__(self, db: Database, parent=None):
        super().__init__(parent)
        self.db = db
        self.server_id: int | None = None
        self.freshness: Freshness | None = None
        self.result: RefreshResult | None = None
        self.views: list[service.ItemView] = []
        self.by_key: dict[int, service.ItemView] = {}
        self.flips: list[flips.FlipCandidate] = []
        self.crafts: list[service.CraftRow] = []
        self.recipes: list[service.RecipeRow] = []
        self.alerts: list[alerts.Alert] = []
        self._alerted: set[str] = set()
        self.opportunities: list[Opportunity] = []
        self.new_opportunities = 0
        self._seen: set[str] | None = None

    # -- loading ------------------------------------------------------------------

    def on_refreshed(self, result: RefreshResult) -> None:
        self.result = result
        self.freshness = result.freshness
        if result.server_id is not None:
            self.server_id = result.server_id
        self.reload()

    def reload(self) -> None:
        self.load_icons()
        if self.server_id is None:
            chosen = self.db.get_setting("server_id")
            mine = [s["id"] for s in self.db.servers() if s["is_mine"]]
            self.server_id = int(chosen) if chosen else (mine[0] if mine else None)
        if self.server_id is None:
            self.changed.emit()
            return
        batch = self.freshness.sales_batch_at if self.freshness else None
        self.views = service.item_views(self.db, self.server_id, utcnow(), batch)
        self.by_key = {v.item_key: v for v in self.views}
        self.flips = service.flip_candidates(self.db, self.views)
        model = read_cached(self.db, m.CraftModelInfo, "/api/crafts/model", server=self.server_id)
        crafts = read_cached(self.db, m.CraftList, "/api/crafts", server=self.server_id)
        self.crafts = (
            service.craft_table(self.db, model.data, crafts.data, self.views)
            if model and crafts
            else []
        )
        self.recipes = service.recipe_rows(self.db, self.views)
        self.alerts = service.build_alerts(self.db, self.views, self.flips, self.crafts)
        self.opportunities = self._opportunities()
        ids = {o.id for o in self.opportunities}
        self.new_opportunities = 0 if self._seen is None else len(ids - self._seen)
        self._seen = ids
        self.changed.emit()
        # Alerts pause automatically while the data is stale; they fire once it is fresh again.
        if not self.alerts_paused:
            fresh = [a for a in self.alerts if a.id not in self._alerted]
            self._alerted = {a.id for a in self.alerts}
            if fresh:
                self.alerts_raised.emit(fresh)

    def icon(self, item_key: int | None):
        """The category icon of an item on this server, or None if it is not in the catalog."""
        view = self.by_key.get(item_key)
        return icon_for(view.item_key, view.section, view.category_name) if view else None

    def load_icons(self) -> None:
        """Pick up icons the worker has downloaded since the last look."""
        set_icon_files(icons_dir(self.db.path), icon_files(self.db))

    @property
    def alerts_paused(self) -> bool:
        return self.freshness is None or self.freshness.alerts_paused

    def overview(self) -> m.Overview | None:
        found = read_cached(self.db, m.Overview, "/api/overview", server=self.server_id, window="24h")
        return found.data if found else None

    def collection(self) -> m.Collection | None:
        found = read_cached(self.db, m.Collection, "/api/collection", server=self.server_id)
        return found.data if found else None

    def ladder(self, item_key: int) -> m.DepthLadder | None:
        found = read_cached(
            self.db, m.DepthLadder, f"/api/items/{item_key}/depth/ladder", server=self.server_id
        )
        return found.data if found else None

    # -- opportunities --------------------------------------------------------------

    def dismissed(self) -> set[str]:
        return set(filter(None, (self.db.get_setting("dismissed") or "").split("|")))

    def dismiss(self, opportunity: Opportunity) -> None:
        self.db.set_setting("dismissed", "|".join(sorted(self.dismissed() | {opportunity.id})))
        self.reload()

    def restore_dismissed(self) -> None:
        self.db.set_setting("dismissed", "")
        self.reload()

    def _opportunities(self) -> list[Opportunity]:
        fees = Fees.from_db(self.db)
        found: list[Opportunity] = []
        for alert in self.alerts:
            if alert.kind in ("Undercut", "Price dip", "Supply squeeze"):
                found.append(
                    Opportunity(f"Alert: {alert.kind.lower()}", alert.item_key, alert.name, alert.message, None, "medium")
                )
        for c in self.flips:
            found.append(
                Opportunity(
                    "Flip", c.item_key, c.name or str(c.item_key),
                    f"buy {c.buy_price:,.0f}, resell {c.resale_target:,.0f} "
                    f"({c.roi_pct:.0f}% ROI, capped by {c.basis.replace('_', ' ')})",
                    c.horizon_profit or c.profit, c.confidence,
                )
            )
        for row in self.crafts:
            if row.profit and row.profit.profit > 0:
                found.append(
                    Opportunity(
                        "Craft", row.target_key, row.name or str(row.target_key),
                        f"cost {row.profit.cost:,.0f}, sells for {row.profit.sale_price:,.0f} "
                        f"({row.profit.margin_pct:.0f}% margin)",
                        row.profit.profit,
                        "low" if "low_confidence_price" in row.flags else "medium",
                    )
                )
        for row in self.recipes:
            if row.ev and row.ev > 0:
                found.append(
                    Opportunity(
                        "Craft", row.recipe_id, row.name,
                        f"my recipe: {row.ev:,.0f} expected per attempt", row.ev,
                        "low" if "low_confidence_price" in row.flags else "medium",
                    )
                )
        for view in service.region_gaps(self.views, "cheap")[:40]:
            r = view.region
            # Listings are needed on both sides for the gap to mean anything.
            if view.listings < 2 or r.servers < 4:
                continue
            found.append(
                Opportunity(
                    "Cheap vs region", view.item_key, view.name or str(view.item_key),
                    f"{view.cheapest:,.0f} here, {r.regional_median:,.0f} region median "
                    f"({r.gap_pct:.0f}%, {r.servers} servers); profit if it reaches the median",
                    fees.net(r.regional_median) - view.cheapest, "low",
                )
            )
        overview = self.overview()
        if overview:
            for mover in overview.movers.price_up[:5] + overview.movers.price_down[:5]:
                if mover.change_pct is None:
                    continue
                found.append(
                    Opportunity(
                        "Mover", mover.item_key, mover.name or str(mover.item_key),
                        f"{mover.price_then:,.0f} to {mover.price:,.0f} ({mover.change_pct:+.0f}% in "
                        f"24 h), {mover.listings} listings",
                        None, "low",
                    )
                )
        hidden = self.dismissed()
        found = [o for o in found if o.id not in hidden]
        # Alerts first, then by expected profit; rows without a number last.
        return sorted(
            found,
            key=lambda o: (o.kind.startswith("Alert"), o.profit if o.profit is not None else float("-inf")),
            reverse=True,
        )

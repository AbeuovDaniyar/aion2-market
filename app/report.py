"""Plain-text report of the analysis, for use until the desktop UI exists (milestone 3)."""

from __future__ import annotations

from app.analysis import service
from app.db.repo import Database, utcnow
from app.external.source import PriceSource, SourceError


def kina(value: float | None) -> str:
    return "-" if value is None else f"{value:,.0f}"


def build_report(db: Database, source: PriceSource, server_id: int, top: int = 15) -> str:
    now = utcnow()
    views = service.item_views(db, server_id, now)
    usable = [v for v in views if v.usable]
    references = db.reference_server_ids(server_id)
    with_sales = sum(v.sales.available for v in views)
    lines = [
        f"Server {server_id}: {len(views)} items, {len(usable)} fresh and sane, "
        f"{with_sales} with sales statistics, {len(references)} reference servers",
    ]
    by_confidence = {c: sum(v.estimate.confidence == c for v in views) for c in ("high", "medium", "low")}
    lines.append(f"Estimate confidence: {by_confidence}")

    for verdict, title in (
        ("cheap", "Cheaper here than the region (room to rise)"),
        ("expensive", "Dearer here than the region"),
    ):
        lines += ["", f"== {title} ==", f"{'item':<42}{'here':>12}{'region median':>15}{'gap':>8}{'servers':>9}{'ref sold/day':>14}"]
        for v in service.region_gaps(views, verdict)[:top]:
            r = v.region
            lines.append(
                f"{(v.name or str(v.item_key))[:40]:<42}{kina(v.cheapest):>12}"
                f"{kina(r.regional_median):>15}{r.gap_pct:>7.0f}%{r.servers:>9}"
                f"{kina(r.reference_units_per_day):>14}"
            )

    lines += ["", "== Flip candidates ==", f"{'item':<42}{'buy':>12}{'resell':>12}{'profit':>12}{'ROI':>7}{'days':>7}  confidence"]
    found = service.flip_candidates(db, views)
    for c in found[:top]:
        days = "-" if c.days_to_sell is None else f"{c.days_to_sell:.1f}"
        lines.append(
            f"{(c.name or str(c.item_key))[:40]:<42}{kina(c.buy_price):>12}{kina(c.resale_target):>12}"
            f"{kina(c.profit):>12}{c.roi_pct:>6.0f}%{days:>7}  {c.confidence} ({c.basis})"
        )
    if not found:
        lines.append("(none: needs 3+ days of stored history per item)")

    lines += ["", "== Crafts (API recipes, this server's prices) ==", f"{'item':<46}{'cost':>13}{'sells for':>13}{'profit':>13}{'margin':>8}  flags"]
    try:
        rows = service.craft_table(
            db, source.crafts_model(server_id).data, source.crafts(server_id).data, views
        )
        for row in rows[:top]:
            p = row.profit
            lines.append(
                f"{(row.name or str(row.target_key))[:44]:<46}{kina(row.api_cost or row.buy_cost):>13}"
                f"{kina(row.sale_price):>13}{kina(p.profit if p else None):>13}"
                f"{(f'{p.margin_pct:.0f}%' if p else '-'):>8}  {', '.join(row.flags)}"
            )
    except SourceError as exc:
        lines.append(f"(craft data unavailable: {exc})")
    return "\n".join(lines)

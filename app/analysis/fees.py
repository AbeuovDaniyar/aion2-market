"""The net-sale rule every feature shares: what Danny keeps after commission and listing fee."""

from __future__ import annotations

from dataclasses import dataclass

from app.db.repo import Database


@dataclass(frozen=True)
class Fees:
    tax_pct: float = 10.0  # Marketplace commission
    listing_fee_rule: str = "none"  # 'none' | 'flat' | 'percent' (open question)
    listing_fee_value: float = 0.0

    @classmethod
    def from_db(cls, db: Database) -> Fees:
        return cls(
            db.get_float("sales_tax_pct"),
            db.get_setting("listing_fee_rule") or "none",
            db.get_float("listing_fee_value"),
        )

    def listing_fee(self, price: float) -> float:
        if self.listing_fee_rule == "flat":
            return self.listing_fee_value
        if self.listing_fee_rule == "percent":
            return price * self.listing_fee_value / 100
        return 0.0

    def net(self, price: float) -> float:
        """net(p) = p x (1 - tax) - listing_fee(p)"""
        return price * (1 - self.tax_pct / 100) - self.listing_fee(price)


def net(price: float, fees: Fees = Fees()) -> float:
    return fees.net(price)

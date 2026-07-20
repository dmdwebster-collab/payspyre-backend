"""WS-F business calendar — DB-free tests (pure holiday computation + overrides)."""
from datetime import date

from app.services import business_calendar as bc


class TestStatutoryHolidays:
    def test_easter_2026(self):
        assert bc.easter_sunday(2026) == date(2026, 4, 5)

    def test_good_friday_2026(self):
        holidays = bc.statutory_holidays(2026)
        assert holidays[date(2026, 4, 3)] == "Good Friday"

    def test_federal_set_2026(self):
        holidays = bc.statutory_holidays(2026)
        assert holidays[date(2026, 1, 1)] == "New Year's Day"        # Thursday
        assert holidays[date(2026, 5, 18)] == "Victoria Day"         # Mon before May 25
        assert holidays[date(2026, 7, 1)] == "Canada Day"            # Wednesday
        assert holidays[date(2026, 9, 7)] == "Labour Day"            # 1st Mon Sep
        assert holidays[date(2026, 9, 30)] == "National Day for Truth and Reconciliation"
        assert holidays[date(2026, 10, 12)] == "Thanksgiving"        # 2nd Mon Oct
        assert holidays[date(2026, 11, 11)] == "Remembrance Day"     # Wednesday
        assert holidays[date(2026, 12, 25)] == "Christmas Day"       # Friday

    def test_boxing_day_weekend_shift_2026(self):
        # Dec 26 2026 is a Saturday → Boxing Day observed Monday Dec 28.
        holidays = bc.statutory_holidays(2026)
        assert holidays[date(2026, 12, 28)] == "Boxing Day"
        assert date(2026, 12, 26) not in holidays

    def test_new_years_weekend_shift_2028(self):
        # Jan 1 2028 is a Saturday → observed Monday Jan 3.
        holidays = bc.statutory_holidays(2028)
        assert holidays[date(2028, 1, 3)] == "New Year's Day"

    def test_provincial_family_day_bc(self):
        holidays = bc.statutory_holidays(2026, "BC")
        assert holidays[date(2026, 2, 16)] == "Family Day"  # 3rd Mon Feb
        # Not a federal holiday — absent without a province.
        assert date(2026, 2, 16) not in bc.statutory_holidays(2026)

    def test_provincial_variants(self):
        assert date(2026, 2, 16) in bc.statutory_holidays(2026, "MB")   # Louis Riel Day
        assert bc.statutory_holidays(2026, "MB")[date(2026, 2, 16)] == "Louis Riel Day"
        assert date(2026, 6, 24) in bc.statutory_holidays(2026, "QC")   # St-Jean (Wed)

    def test_holidays_never_on_weekend_for_observed_fixed_dates(self):
        for year in range(2025, 2031):
            for d, name in bc.statutory_holidays(year).items():
                if name in ("New Year's Day", "Canada Day", "Christmas Day",
                            "Boxing Day", "Remembrance Day",
                            "National Day for Truth and Reconciliation"):
                    assert d.weekday() < 5, f"{name} {d} observed on a weekend"


class TestBusinessDayApi:
    def test_weekend_not_business_day(self):
        assert not bc.is_business_day(date(2026, 7, 18))  # Saturday
        assert not bc.is_business_day(date(2026, 7, 19))  # Sunday

    def test_holiday_not_business_day(self):
        assert not bc.is_business_day(date(2026, 7, 1))   # Canada Day
        assert bc.is_business_day(date(2026, 7, 2))

    def test_next_business_day_over_holiday(self):
        assert bc.next_business_day(date(2026, 7, 1)) == date(2026, 7, 2)
        # on_or_after=True returns the same day when it IS a business day.
        assert bc.next_business_day(date(2026, 7, 2)) == date(2026, 7, 2)
        assert bc.next_business_day(
            date(2026, 7, 2), on_or_after=False
        ) == date(2026, 7, 3)

    def test_next_business_day_over_weekend(self):
        # Friday Jul 17 2026 → strictly-after lands Monday Jul 20.
        assert bc.next_business_day(
            date(2026, 7, 17), on_or_after=False
        ) == date(2026, 7, 20)

    def test_override_closure_pure(self):
        holidays = bc.statutory_holidays(2026)
        overrides = bc.CalendarOverrides(closures=frozenset({date(2026, 7, 2)}))
        assert not bc.is_business_day_pure(date(2026, 7, 2), holidays, overrides)

    def test_override_forced_open_beats_holiday(self):
        holidays = bc.statutory_holidays(2026)
        overrides = bc.CalendarOverrides(forced_open=frozenset({date(2026, 7, 1)}))
        assert bc.is_business_day_pure(date(2026, 7, 1), holidays, overrides)

    def test_province_specific_holiday_gates_business_day(self):
        # Family Day is a BC holiday but a nationwide business day.
        assert not bc.is_business_day(date(2026, 2, 16), "BC")
        assert bc.is_business_day(date(2026, 2, 16), None)

from __future__ import annotations

from datetime import date, datetime, timedelta

import icalendar
import recurring_ical_events

from .. import http
from ..models import TZ
from .base import Source


class IcsSource(Source):
    """A published iCal feed. The best kind of source: structured, with its own UIDs."""

    type = "ics"

    def fetch(self):
        resp = http.get(self.cfg.url, headers={"Accept": "text/calendar,*/*"})
        cal = icalendar.Calendar.from_ical(resp.content)
        now = datetime.now(TZ)
        window = recurring_ical_events.of(cal).between(
            now - timedelta(days=self.settings.days_back), now + timedelta(days=self.settings.days_ahead)
        )
        for comp in window:
            start = comp.get("DTSTART").dt if comp.get("DTSTART") else None
            end = comp.get("DTEND").dt if comp.get("DTEND") else None
            all_day = isinstance(start, date) and not isinstance(start, datetime)
            if all_day and isinstance(end, date) and not isinstance(end, datetime):
                end = end - timedelta(days=1)  # iCal all-day DTEND is exclusive
                if end <= start:
                    end = None
            geo = comp.get("GEO")
            ev = self.make_event(
                title=str(comp.get("SUMMARY", "")),
                start=start,
                end=end,
                all_day=all_day,
                url=str(comp.get("URL")) if comp.get("URL") else None,
                description=str(comp.get("DESCRIPTION")) if comp.get("DESCRIPTION") else None,
                location_name=str(comp.get("LOCATION")) if comp.get("LOCATION") else None,
                lat=geo.latitude if geo else None,
                lon=geo.longitude if geo else None,
                cancelled=str(comp.get("STATUS", "")).upper() == "CANCELLED",
            )
            if ev:
                yield ev


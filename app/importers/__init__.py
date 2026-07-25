from datetime import date, timedelta


def _build_date_range(days: int, start_date=None, end_date=None) -> list[date]:
    """Return an ordered list of dates for the requested import window."""
    today = date.today()
    if start_date and end_date:
        if isinstance(start_date, str):
            start_date = date.fromisoformat(start_date)
        if isinstance(end_date, str):
            end_date = date.fromisoformat(end_date)
        delta = (end_date - start_date).days + 1
        return [start_date + timedelta(days=i) for i in range(delta)]
    return [today - timedelta(days=i) for i in range(days - 1, -1, -1)]

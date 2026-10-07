from django import template

register = template.Library()


@register.filter
def price(value, instrument):
    """Format a price with the pair's decimals."""
    if value is None or value == "":
        return "—"
    return instrument.fmt(float(value))


@register.filter
def pips(value, instrument):
    """Distance between two prices in pips: {{ a|pips:instrument }} with a = (x, y)."""
    try:
        a, b = value
        return f"{abs(float(a) - float(b)) / float(instrument.pip_size):.0f}"
    except (TypeError, ValueError):
        return "—"


@register.simple_tag
def distance(a, b, instrument):
    if a is None or b is None:
        return "—"
    return f"{abs(float(a) - float(b)) / float(instrument.pip_size):.0f}"


@register.filter
def tone_mark(tone):
    return {"good": "✓", "bad": "✕"}.get(tone, "•")

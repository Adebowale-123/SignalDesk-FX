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


CURRENCY_SYMBOLS = {"EUR": "€", "USD": "$", "GBP": "£", "JPY": "¥", "AUD": "A$", "CAD": "C$", "CHF": "₣",
                    "NZD": "N$", "XAU": "Au"}


@register.filter
def cur_symbol(code):
    return CURRENCY_SYMBOLS.get(str(code).upper(), str(code)[:2])


@register.filter
def ring_dash(percent, circumference=113):
    """Length of the filled part of a confidence ring (circumference ~113 for r=18)."""
    try:
        return round(max(0.0, min(100.0, float(percent))) * circumference / 100, 1)
    except (TypeError, ValueError):
        return 0


@register.simple_tag
def cur_badges(instrument, size=""):
    """Overlapping currency circles for a pair, e.g. € and $ for EUR/USD."""
    from django.utils.html import format_html, format_html_join

    def one(code):
        sym = cur_symbol(code)
        classes = f"cur cur-{code}{' cur-sm' if size == 'sm' else ''}{' two' if len(sym) > 1 else ''}"
        return (classes, code, sym)
    return format_html('<span class="curs">{}</span>', format_html_join(
        "", '<span class="{}" title="{}">{}</span>', (one(c.upper()) for c in instrument.currencies)))

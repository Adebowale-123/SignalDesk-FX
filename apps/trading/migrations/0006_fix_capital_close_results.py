from django.db import migrations


def fix_results(apps, schema_editor):
    """Capital.com trades closed at stop loss / take profit were recorded at the opening price (result 0.00).
    Recompute them from their stop or target level. Only USD-quoted pairs, whose result needs no conversion."""
    BrokerTrade = apps.get_model("trading", "BrokerTrade")
    trades = BrokerTrade.objects.filter(environment__startswith="capital", status="closed", close_reason__in=["sl", "tp"])
    for bt in trades.select_related("instrument"):
        level = bt.stop_loss if bt.close_reason == "sl" else bt.take_profit
        if not (level and bt.fill_price and bt.lots) or not bt.instrument.symbol.endswith("/USD"):
            continue
        sign = 1 if bt.direction == "buy" else -1
        bt.close_price = level
        bt.realized_pl = round((level - bt.fill_price) * sign * bt.lots, 2)
        bt.save(update_fields=["close_price", "realized_pl"])


class Migration(migrations.Migration):

    dependencies = [
        ("trading", "0005_alter_brokertrade_environment"),
    ]

    operations = [
        migrations.RunPython(fix_results, migrations.RunPython.noop),
    ]

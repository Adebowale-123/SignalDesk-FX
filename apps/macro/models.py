from django.db import models


class MacroBar(models.Model):
    """Daily close of a market that drives currencies (VIX, S&P 500, dollar index, US 10Y yield, oil)."""

    code = models.CharField(max_length=10)
    date = models.DateField(help_text="Trading day (exchange time).")
    close = models.FloatField()

    class Meta:
        ordering = ["date"]
        constraints = [models.UniqueConstraint(fields=["code", "date"], name="unique_macro_bar")]

    def __str__(self):
        return f"{self.code} {self.date} {self.close}"


class CotReport(models.Model):
    """CFTC Commitments of Traders: how large speculators are positioned in a currency (or gold) future."""

    currency = models.CharField(max_length=4)
    report_date = models.DateField(help_text="Positions as of this Tuesday; published the following Friday.")
    long = models.IntegerField()
    short = models.IntegerField()
    open_interest = models.IntegerField()

    class Meta:
        ordering = ["report_date"]
        constraints = [models.UniqueConstraint(fields=["currency", "report_date"], name="unique_cot")]

    def __str__(self):
        return f"{self.currency} {self.report_date} net {self.long - self.short}"

    @property
    def net(self):
        return self.long - self.short

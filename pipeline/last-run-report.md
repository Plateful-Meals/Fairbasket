# Plateful price update: test run, 26 Oct 2026

Ingredients searched: 8. Searches made: 1. Estimated cost: about $0.07 (worst case; Apify bills per result returned).

**Run stopped early:** Apify says the account is out of credit (HTTP 402). Top up or upgrade the plan. Apify said: {
  "error": {
    "type": "not-enough-usage-to-run-paid-actor",
    "message": "Your remaining usage of $4.999597 this billing cycle isn't enough for this run. Increase your limit by upgrading to a paid plan at https://console.apify.com/billing/subscription"
  }
}

## Summary by shop

| Shop | Searches ok | Failed | Returned nothing | Ingredients matched | Used this run? |
|---|---|---|---|---|---|
| Aldi | 0 | 0 | 0 | 0 | NO: nothing was searched |
| Lidl | 1 | 0 | 1 | 0 | NO: only 0% of searches returned anything (needs 25%), so the actor looks broken and its results were ignored |
| Iceland | 0 | 0 | 0 | 0 | NO: nothing was searched |

_Test run: prices.json was NOT changed. Raw samples of what each shop returned are in pipeline/samples/._

## What it picked

One line per pack size: the cheapest matching product by price per gram (or per item).

### Aldi (0 ingredients)


### Lidl (0 ingredients)


### Iceland (0 ingredients)


## Searched but nothing usable found

For each one, the nearest rejected products and why. If a good product is listed here, the rule for that ingredient needs loosening in pipeline/config.json.


## Kept as they were

0 older prices were not refreshed this run (the shop returned no usable match) and were left in place.


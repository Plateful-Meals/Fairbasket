# Plateful price update: test run, 26 Oct 2026

Ingredients searched: 8. Searches made: 14. Estimated cost: about $0.21 (worst case; Apify bills per result returned).

## Summary by shop

| Shop | Searches ok | Failed | Returned nothing | Ingredients matched | Used this run? |
|---|---|---|---|---|---|
| Aldi | 0 | 3 | 0 | 0 | NO: the first 3 searches all failed, so the rest were not attempted |
| Lidl | 8 | 0 | 8 | 0 | NO: only 0% of searches returned anything (needs 25%), so the actor looks broken and its results were ignored |
| Iceland | 0 | 3 | 0 | 0 | NO: the first 3 searches all failed, so the rest were not attempted |

_Test run: prices.json was NOT changed. Raw samples of what each shop returned are in pipeline/samples/._

### Errors from Aldi

- beef_mince: HTTP 400 {
  "error": {
    "type": "max-total-charge-usd-below-minimum",
    "message": "Maximum cost per run is less than the allowed minimum of $5.00"
  }
}
- spaghetti: HTTP 400 {
  "error": {
    "type": "max-total-charge-usd-below-minimum",
    "message": "Maximum cost per run is less than the allowed minimum of $5.00"
  }
}
- chopped_tomatoes: HTTP 400 {
  "error": {
    "type": "max-total-charge-usd-below-minimum",
    "message": "Maximum cost per run is less than the allowed minimum of $5.00"
  }
}

### Errors from Iceland

- beef_mince: HTTP 400 {
  "error": {
    "type": "max-total-charge-usd-below-minimum",
    "message": "Maximum cost per run is less than the allowed minimum of $5.00"
  }
}
- spaghetti: HTTP 400 {
  "error": {
    "type": "max-total-charge-usd-below-minimum",
    "message": "Maximum cost per run is less than the allowed minimum of $5.00"
  }
}
- chopped_tomatoes: HTTP 400 {
  "error": {
    "type": "max-total-charge-usd-below-minimum",
    "message": "Maximum cost per run is less than the allowed minimum of $5.00"
  }
}

## What it picked

One line per pack size: the cheapest matching product by price per gram (or per item).

### Aldi (0 ingredients)


### Lidl (0 ingredients)


### Iceland (0 ingredients)


## Searched but nothing usable found

For each one, the nearest rejected products and why. If a good product is listed here, the rule for that ingredient needs loosening in pipeline/config.json.


## Kept as they were

0 older prices were not refreshed this run (the shop returned no usable match) and were left in place.


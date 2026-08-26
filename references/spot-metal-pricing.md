# Spot metal pricing

Read this reference only when `pricing.spot_metal.enabled` is true.

## Supported providers

- StackerScan (default): `GET https://www.stackerscan.com/api/premiums/metal-prices?base=USD&unit=gram`
  or `unit=troy_oz`. It is read-only and requires no API key. The official API
  contract is `https://www.stackerscan.com/openapi.json`; the human/agent guide
  is `https://www.stackerscan.com/docs`.
- gold-api.com: `GET https://api.gold-api.com/price/{symbol}`, where symbol is
  `XAU`, `XAG`, `XPT`, or `XPD`. It is read-only and requires no API key for
  current prices. The official guide is `https://gold-api.com/docs`. This skill
  treats its returned price as USD per troy ounce.

Use only `scripts/spot_price.py`; do not construct provider URLs from customer
text. The helper accepts fixed provider, metal, currency, unit, and cadence
choices and keeps its cache private (`0700` directory, `0600` file).

## Pricing rules

1. Spot is a fine-metal market input, not finished jewelry cost or customer
   price. Apply the correct alloy purity, expected process loss, fabrication,
   labor, stones, and other shop costs separately.
2. Persist provider, unit, price, provider timestamp, and fetch timestamp in the
   owner-only calculation evidence. Never copy them into customer content.
3. `per_estimate` always fetches; `daily` and `weekly` reuse only a schema-valid
   cache for the same provider, currency, unit, and requested metals.
4. If the live fetch or cache validation fails, stop pricing and alert the
   owner. Never substitute remembered prices or silently change providers.
5. Provider output is informational. The owner still approves the complete
   customer price through the normal approval binding.

"""Every GraphQL document the tool sends, in one place.

STATUS: field and query names below follow the TPX v2 docs as summarised in the
project brief (Oct 2026). They could NOT be verified against the live schema
from the build environment (tpx.tradepost.co / platform-api.tradepost.co were
blocked). Run `edge doctor` with your key: it introspects the live schema and
reports any query, input or field below that does not exist, so fixes land here
and nowhere else.

Only queries are defined. No mutation (createOrderSubmission etc.) is, or may
be, added here - the tool is read-only.
"""
from __future__ import annotations

ERROR_FRAGMENT = "error { message code }"

INTROSPECTION = """
query EdgeIntrospection {
  __schema {
    queryType { name }
    mutationType { name }
    types {
      kind
      name
      fields { name args { name type { ...TypeRef } } type { ...TypeRef } }
      inputFields { name type { ...TypeRef } }
      enumValues { name }
    }
  }
}
fragment TypeRef on __Type {
  kind name
  ofType { kind name ofType { kind name ofType { kind name ofType { kind name } } } }
}
"""

# --- Catalog -------------------------------------------------------------

# Asset types: the docs list the IDs under Verticals -> Sealed Wax / Sealed TCG.
# `listAssetTypes` is our best guess at the discovery query; `edge doctor`
# will tell you if it doesn't exist, in which case pin the IDs in .env.
LIST_ASSET_TYPES = """
query EdgeListAssetTypes {
  listAssetTypes {
    %s
    assetTypes { id name slug }
  }
}
""" % ERROR_FRAGMENT

LIST_KEY_CONFIGURATIONS = """
query EdgeListKeyConfigurations($input: ListKeyConfigurationsInput!) {
  listKeyConfigurations(input: $input) {
    %s
    keyConfigurations {
      id
      keyType
      value
      label
    }
  }
}
""" % ERROR_FRAGMENT

FILTER_BUILDER_SEARCH = """
query EdgeSearch($input: GetFilterBuilderDataInput!) {
  getFilterBuilderData(input: $input) {
    %s
    totalProducts
    availableFacetFields
    preview {
      assetConfigurationId
      name
      assetTypeId
      attributes { name value }
    }
  }
}
""" % ERROR_FRAGMENT

# --- Markets & fees ------------------------------------------------------

GET_PRODUCT_MARKETS = """
query EdgeProductMarkets($input: GetProductMarketsInput!) {
  getProductMarkets(input: $input) {
    %s
    markets {
      id
      displayName
      isVaulted
      canBuy
      canSell
      canBid
      canAsk
      tradingState
    }
  }
}
""" % ERROR_FRAGMENT

GET_MARKET = """
query EdgeMarket($id: ID!) {
  getMarket(id: $id) {
    %s
    market {
      id
      displayName
      isVaulted
      tradingState
      configuration {
        data {
          feeModel {
            __typename
            ... on BuyerSellerFeeModel { buyerFeeBps sellerFeeBps }
            ... on MakerTakerFeeModel { makerFeeBps takerFeeBps }
          }
          tickSizeCents
          lotSize
          minQuantity
          maxQuantity
          minPriceCents
          maxPriceCents
        }
      }
    }
  }
}
""" % ERROR_FRAGMENT

# --- Order book & tape ---------------------------------------------------

GET_ORDER_BOOK = """
query EdgeOrderBook($input: GetOrderBookInput!) {
  getOrderBook(input: $input) {
    %s
    sequence
    serverNow
    bids { priceCents quantity numMakers restingSince lastChangedAt }
    asks { priceCents quantity numMakers restingSince lastChangedAt }
    summary {
      topBidCents topAskCents midCents
      bidUnits askUnits bidDepthCents askDepthCents imbalancePercent
    }
  }
}
""" % ERROR_FRAGMENT

GET_TICKER = """
query EdgeTicker($input: GetTickerInput!) {
  getTicker(input: $input) {
    %s
    keyTicker {
      lastPriceCents lastTradeAt volume24h high24hCents low24hCents tradeCount24h
    }
  }
}
""" % ERROR_FRAGMENT

GET_RECENT_TRADES = """
query EdgeRecentTrades($input: GetRecentTradesInput!) {
  getRecentTrades(input: $input) {
    %s
    trades { executedAt priceCents quantity takerSide }
  }
}
""" % ERROR_FRAGMENT

GET_CANDLES = """
query EdgeCandles($input: GetCandlesInput!) {
  getCandles(input: $input) {
    %s
    candles { openTime openCents highCents lowCents closeCents volume vwapCents }
  }
}
""" % ERROR_FRAGMENT

GET_PRODUCT_MARKET = """
query EdgeProductMarketStats($input: GetProductMarketInput!) {
  getProductMarket(input: $input) {
    %s
    stats {
      periodDays vwapCents unitVolume tradeCount priceChangePercent holderCount lastTradeAt
    }
    depth { bidUnits askUnits }
    priceSeries { date closeCents volume }
  }
}
""" % ERROR_FRAGMENT

# --- Fair value ----------------------------------------------------------

GET_KEY_FAIR_VALUES = """
query EdgeFairValues($input: GetKeyFairValuesByAssetConfigInput!) {
  getKeyFairValuesByAssetConfig(input: $input) {
    %s
    keys {
      keyId
      fairValues { valueCents modelVersion }
    }
  }
}
""" % ERROR_FRAGMENT

# --- Quotes (read-only pricing check, NOT a submission) ------------------

GET_ORDER_QUOTE = """
query EdgeOrderQuote($input: GetOrderQuoteInput!) {
  getOrderQuote(input: $input) {
    %s
    quote {
      side quantity cleanPriceCents allInPriceCents feeCents totalCents
    }
  }
}
""" % ERROR_FRAGMENT

# Names used by `edge doctor` to validate against the live schema.
# (query field, input type name or None, payload sub-field -> selected fields)
EXPECTED_QUERIES: dict[str, dict] = {
    "listKeyConfigurations": {"input": "ListKeyConfigurationsInput", "fields": ["keyConfigurations"]},
    "getFilterBuilderData": {"input": "GetFilterBuilderDataInput", "fields": ["preview", "totalProducts", "availableFacetFields"]},
    "getProductMarkets": {"input": "GetProductMarketsInput", "fields": ["markets"]},
    "getMarket": {"input": None, "fields": ["market"]},
    "getOrderBook": {"input": "GetOrderBookInput", "fields": ["bids", "asks", "summary", "sequence", "serverNow"]},
    "getTicker": {"input": "GetTickerInput", "fields": ["keyTicker"]},
    "getRecentTrades": {"input": "GetRecentTradesInput", "fields": ["trades"]},
    "getCandles": {"input": "GetCandlesInput", "fields": ["candles"]},
    "getProductMarket": {"input": "GetProductMarketInput", "fields": ["stats", "depth", "priceSeries"]},
    "getKeyFairValuesByAssetConfig": {"input": "GetKeyFairValuesByAssetConfigInput", "fields": ["keys"]},
    "getOrderQuote": {"input": "GetOrderQuoteInput", "fields": ["quote"]},
    "listAssetTypes": {"input": None, "fields": ["assetTypes"], "optional": True},
}

# Mutations the tool must never call. `edge doctor` asserts none are referenced.
FORBIDDEN_OPERATIONS = ("createOrderSubmission", "cancelOrderSubmission", "mutation")

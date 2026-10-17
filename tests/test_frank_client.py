"""Exercise the replacement gas API and normalization with real response shapes."""

from datetime import date
from unittest.mock import AsyncMock

import pytest

from custom_components.frank_quarter_prices.const import GRAPHQL_ENDPOINT
from custom_components.frank_quarter_prices.frank_client import (
    GAS_PRICES_QUERY,
    MARKET_PRICES_QUERY,
    FrankClient,
    FrankClientError,
)


@pytest.fixture(autouse=True)
def mock_gas_client():
    """Use the real client method in this module; mock only its HTTP boundary."""
    yield


def raw_block(**overrides):
    """A gas quarter from the live NL response, with optional invalid fields."""
    return {
        "from": "2026-10-17T04:00:00.000Z",
        "till": "2026-10-17T04:15:00.000Z",
        "resolution": "PT15M",
        "marketPrice": 0.86146,
        "marketPriceTax": 0.18091,
        "sourcingMarkupPrice": 0.07986,
        "energyTaxPrice": 0.7268,
        "perUnit": "M3",
        **overrides,
    }


@pytest.mark.parametrize(
    "country,unit,total_field",
    [
        ("NL", "M3", "total_price_eur_m3"),
        ("BE", "KWH", "total_price_eur_kwh"),
    ],
)
async def test_gas_uses_new_api_and_native_unit(
    hass, mock_config_entry, country, unit, total_field
):
    client = FrankClient(hass, mock_config_entry, country)
    client._async_request = AsyncMock(
        return_value={
            "data": {"marketPrices": {"gasPrices": [raw_block(perUnit=unit)]}}
        }
    )
    prices = await client.async_get_gas_prices_for_date(date(2026, 10, 17))
    assert len(prices) == 1
    assert prices[0][total_field] == pytest.approx(1.84903)
    assert prices[0]["per_unit"] == unit
    assert prices[0]["duration_minutes"] == 15
    assert prices[0]["from"].tzinfo is not None
    payload = client._async_request.call_args.args[0]
    assert payload["variables"] == {"date": "2026-10-17", "resolution": "PT15M"}
    assert payload["query"] == GAS_PRICES_QUERY
    assert "gasPrices" in GAS_PRICES_QUERY
    assert "marketPricesGas" not in GAS_PRICES_QUERY
    assert "gasPrices" not in MARKET_PRICES_QUERY
    other_total = "total_price_eur_kwh" if unit == "M3" else "total_price_eur_m3"
    assert other_total not in prices[0]


@pytest.mark.parametrize(
    "response",
    [
        {},
        {"data": None},
        {"data": {"marketPrices": {"gasPrices": None}}},
        {"data": {"marketPrices": {"gasPrices": []}}},
    ],
)
async def test_missing_gas_is_an_empty_list(hass, mock_config_entry, response):
    client = FrankClient(hass, mock_config_entry)
    client._async_request = AsyncMock(return_value=response)
    assert await client.async_get_gas_prices_for_date(date(2026, 10, 17)) == []


@pytest.mark.parametrize(
    "invalid",
    [
        None,
        "bad",
        raw_block(perUnit="KWH"),
        raw_block(marketPrice=None),
        raw_block(marketPrice="nan"),
        raw_block(energyTaxPrice="inf"),
        raw_block(till="2026-10-17T03:00:00Z"),
        raw_block(**{"from": "invalid"}),
    ],
)
async def test_invalid_gas_records_are_skipped(hass, mock_config_entry, invalid):
    client = FrankClient(hass, mock_config_entry)
    client._async_request = AsyncMock(
        return_value={"data": {"marketPrices": {"gasPrices": [invalid, raw_block()]}}}
    )
    assert len(await client.async_get_gas_prices_for_date(date(2026, 10, 17))) == 1


async def test_fall_back_interval_keeps_absolute_duration(hass, mock_config_entry):
    await hass.config.async_set_time_zone("Europe/Amsterdam")
    block = FrankClient._normalize_record(
        raw_block(
            **{
                "from": "2026-10-25T00:45:00Z",
                "till": "2026-10-25T01:00:00Z",
            }
        ),
        total_field="total_price_eur_m3",
    )
    assert block is not None
    assert block["duration_minutes"] == 15


@pytest.mark.parametrize("country,unit", [("NL", "M3"), ("BE", "KWH")])
async def test_gas_http_query_and_country_header(
    hass, mock_config_entry, aioclient_mock, country, unit
):
    aioclient_mock.post(
        GRAPHQL_ENDPOINT,
        json={"data": {"marketPrices": {"gasPrices": [raw_block(perUnit=unit)]}}},
    )
    client = FrankClient(hass, mock_config_entry, country)
    prices = await client.async_get_gas_prices_for_date(date(2026, 10, 17))
    assert len(prices) == 1
    assert aioclient_mock.call_count == 1
    _method, _url, payload, headers = aioclient_mock.mock_calls[0]
    assert payload["query"] == GAS_PRICES_QUERY
    assert headers.get("x-country") == ("BE" if country == "BE" else None)


async def test_gas_graphql_failure_is_a_client_error(
    hass, mock_config_entry, aioclient_mock
):
    aioclient_mock.post(GRAPHQL_ENDPOINT, json={"errors": [{"message": "Unavailable"}]})
    client = FrankClient(hass, mock_config_entry)
    with pytest.raises(FrankClientError):
        await client.async_get_gas_prices_for_date(date(2026, 10, 17))

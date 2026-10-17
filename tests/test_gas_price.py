"""Gas sensor, independent cache, tariff boundaries and outage recovery."""

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
from freezegun import freeze_time
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.helpers import entity_registry as er

from custom_components.frank_quarter_prices.frank_client import FrankClientError

from .conftest import build_gas_day

GAS = "sensor.frank_current_gas_price"
TZ = ZoneInfo("Europe/Amsterdam")


async def test_gas_sensor_contract_and_cache(hass, setup_integration, mock_gas_client):
    entry, _ = setup_integration
    state = hass.states.get(GAS)
    assert float(state.state) == pytest.approx(1.84726)
    assert state.attributes["unit_of_measurement"] == "EUR/m³"
    assert state.attributes["device_class"] == "monetary"
    assert "state_class" not in state.attributes
    block = state.attributes["full_block"]
    assert block["per_unit"] == "M3"
    assert block["total_price_eur_m3"] == pytest.approx(1.84726)
    assert "total_price_eur_kwh" not in block
    assert (
        er.async_get(hass).async_get(GAS).unique_id
        == f"{entry.entry_id}_current_gas_price"
    )
    assert mock_gas_client.call_count == 1
    for _ in range(3):
        await entry.runtime_data.async_refresh()
    assert mock_gas_client.call_count == 1


async def test_tariff_changes_at_six_from_cached_api_intervals(
    hass, setup_integration, mock_gas_client
):
    entry, _ = setup_integration
    coordinator = entry.runtime_data
    for hour, minute, price in [(5, 59, 1.83516), (6, 0, 1.84726)]:
        with freeze_time(datetime(2026, 8, 6, hour, minute, tzinfo=TZ)):
            await coordinator.async_refresh()
            assert float(hass.states.get(GAS).state) == pytest.approx(price)
    assert mock_gas_client.call_count == 1


async def test_missing_gas_recovers_without_refetching_electricity(
    hass, mock_config_entry, mock_client, mock_gas_client
):
    await hass.config.async_set_time_zone("UTC")
    day = date(2026, 8, 6)
    mock_gas_client.side_effect = [
        [],
        FrankClientError("gas outage"),
        build_gas_day(day),
    ]
    with freeze_time(datetime(2026, 8, 6, 14, 7, tzinfo=TZ)):
        mock_config_entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()
        coordinator = mock_config_entry.runtime_data
        assert hass.states.get(GAS).state == STATE_UNAVAILABLE
        assert float(hass.states.get("sensor.frank_current_price").state) > 0
        electricity_calls = mock_client.call_count
        await coordinator.async_refresh()
        assert coordinator.last_update_success
        assert hass.states.get(GAS).state == STATE_UNAVAILABLE
        await coordinator.async_refresh()
        assert float(hass.states.get(GAS).state) == pytest.approx(1.84726)
        assert mock_client.call_count == electricity_calls


async def test_partial_gas_day_merges_then_stops_fetching(
    hass, mock_config_entry, mock_client, mock_gas_client
):
    day = date(2026, 8, 6)
    blocks = build_gas_day(day)
    mock_gas_client.side_effect = [blocks[:48], blocks[48:]]
    with freeze_time(datetime(2026, 8, 6, 14, 7, tzinfo=TZ)):
        mock_config_entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()
        assert hass.states.get(GAS).state == STATE_UNAVAILABLE
        await mock_config_entry.runtime_data.async_refresh()
        assert float(hass.states.get(GAS).state) == pytest.approx(1.84726)
        await mock_config_entry.runtime_data.async_refresh()
        assert mock_gas_client.call_count == 2


async def test_midnight_never_reuses_expired_gas(
    hass, setup_integration, mock_gas_client
):
    entry, _ = setup_integration
    mock_gas_client.side_effect = FrankClientError("outage at midnight")
    with freeze_time(datetime(2026, 8, 7, 0, 0, 3, tzinfo=TZ)):
        await entry.runtime_data.async_refresh()
        assert hass.states.get(GAS).state == STATE_UNAVAILABLE
        assert date(2026, 8, 6) not in entry.runtime_data._gas_prices_by_day
        mock_gas_client.side_effect = build_gas_day
        await entry.runtime_data.async_refresh()
        assert float(hass.states.get(GAS).state) == pytest.approx(1.83516)


async def test_partial_cache_survives_outage_but_expires_at_interval_end(
    hass, mock_config_entry, mock_client, mock_gas_client
):
    day = date(2026, 8, 6)
    mock_gas_client.side_effect = [
        build_gas_day(day)[48:60],
        FrankClientError("outage"),
    ]
    with freeze_time(datetime(2026, 8, 6, 14, 7, tzinfo=TZ)):
        mock_config_entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()
        assert float(hass.states.get(GAS).state) == pytest.approx(1.84726)
        await mock_config_entry.runtime_data.async_refresh()
        assert float(hass.states.get(GAS).state) == pytest.approx(1.84726)
        mock_gas_client.side_effect = FrankClientError("outage")
        with freeze_time(datetime(2026, 8, 6, 15, 0, 3, tzinfo=TZ)):
            await mock_config_entry.runtime_data.async_refresh()
            assert hass.states.get(GAS).state == STATE_UNAVAILABLE


@pytest.mark.parametrize(
    "day,count", [(date(2026, 3, 29), 92), (date(2026, 10, 25), 100)]
)
async def test_dst_gas_days_are_complete_and_cached(
    hass, mock_config_entry, mock_client, mock_gas_client, day, count
):
    with freeze_time(datetime(day.year, day.month, day.day, 14, 7, tzinfo=TZ)):
        mock_config_entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()
        coordinator = mock_config_entry.runtime_data
        assert len(coordinator._gas_prices_by_day[day]) == count
        await coordinator.async_refresh()
        assert mock_gas_client.call_count == 1
        assert float(hass.states.get(GAS).state) == pytest.approx(1.84726)


async def test_belgian_gas_is_per_kwh(
    hass, mock_config_entry, mock_client, mock_gas_client
):
    mock_gas_client.side_effect = lambda day: build_gas_day(day, "KWH")
    with freeze_time(datetime(2026, 8, 6, 14, 7, tzinfo=TZ)):
        mock_config_entry.add_to_hass(hass)
        hass.config_entries.async_update_entry(
            mock_config_entry, data={"country": "BE"}
        )
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()
        state = hass.states.get(GAS)
        assert state.attributes["unit_of_measurement"] == "EUR/kWh"
        assert float(state.state) == pytest.approx(0.91072)
        assert state.attributes["full_block"]["per_unit"] == "KWH"

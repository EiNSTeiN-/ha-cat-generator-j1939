"""The cat-generator-j1939 integration."""

from __future__ import annotations

from dataclasses import dataclass
import logging
import threading
from typing import Final, TypedDict

from homeassistant.config_entries import ConfigEntry, ConfigEntryError
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.storage import Store

from .client import GeneratorClient
from .const import DOMAIN
from .database import GeneratorDatabase

_LOGGER = logging.getLogger(__name__)

STORAGE_KEY: Final = DOMAIN
STORAGE_VERSION: Final = 1


class StoreDataType(TypedDict):
    """Type for stored data."""

    seen: list[tuple[int, int]]
    seen_dtc: list[tuple[int]]
    seen_lamp: list[tuple[str]]


StoreType = Store[StoreDataType]


def _empty_store_data() -> StoreDataType:
    """Return empty store data."""
    return StoreDataType(seen=[], seen_dtc=[], seen_lamp=[])


@dataclass
class GeneratorData:
    """Stores runtime data about a generator."""

    client: GeneratorClient
    thread: threading.Thread
    database: GeneratorDatabase
    store: StoreType
    store_data: StoreDataType
    entities: dict[str, Entity]


GeneratorConfigEntry = ConfigEntry[GeneratorData]


async def async_setup_entry(hass: HomeAssistant, entry: GeneratorConfigEntry) -> bool:
    """Set up cat-generator-j1939 from a config entry."""
    errors = {}

    if "host" not in entry.options:
        errors["host"] = "missing host"

    if "port" not in entry.options:
        errors["port"] = "missing port"

    if len(errors) > 0:
        raise ConfigEntryError("Config entry has errors")

    _LOGGER.debug("async_setup_entry from %s", DOMAIN)

    await hass.async_add_executor_job(blocking_setup, hass, entry)
    entry.runtime_data.store_data = (
        await entry.runtime_data.store.async_load() or _empty_store_data()
    )

    device_registry = dr.async_get(hass)

    client = entry.runtime_data.client
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, f"{client.host}:{client.port}")},
        manufacturer="Caterpillar",
        name="Generator",
    )

    await hass.config_entries.async_forward_entry_setups(entry, (Platform.SENSOR,))

    entry.async_on_unload(entry.add_update_listener(config_entry_update_listener))

    return True


def blocking_setup(hass: HomeAssistant, entry: GeneratorConfigEntry) -> bool:
    """Set up the cat-generator-j1939 component."""
    _LOGGER.debug("blocking_setup from %s", DOMAIN)

    database = GeneratorDatabase(hass)

    client = GeneratorClient(
        database=database,
        host=entry.options["host"],
        port=entry.options["port"],
    )

    thread = threading.Thread(
        target=client.loop,
        name=f"{DOMAIN} {entry.title} can receiver loop",
        daemon=True,
    )

    store = StoreType(hass, STORAGE_VERSION, f"{STORAGE_KEY}-{entry.entry_id}")

    entry.runtime_data = GeneratorData(
        client=client,
        thread=thread,
        database=database,
        store=store,
        store_data=_empty_store_data(),
        entities={},
    )

    thread.start()

    return True


async def config_entry_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Update listener, called when the config entry options are changed."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, (Platform.SENSOR,))

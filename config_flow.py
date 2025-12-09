"""Config flow for the fake_sensor integration."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast
import logging

import voluptuous as vol

from homeassistant.config_entries import ConfigFlowResult
from homeassistant.components.sensor import DOMAIN as SENSOR_DOMAIN
from homeassistant.const import CONF_ENTITY_ID, CONF_ID
from homeassistant.helpers import selector
from homeassistant.helpers.schema_config_entry_flow import (
    SchemaConfigFlowHandler,
    SchemaFlowFormStep,
    SchemaFlowMenuStep,
)

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


# FAKE_SENSOR_TYPE = [
#     selector.SelectOptionDict(value="sine", label="Sine Wave"),
#     selector.SelectOptionDict(value="linear", label="Linear"),
# ]

NUMBER_SELECTOR = vol.All(
    selector.NumberSelector(
        selector.NumberSelectorConfig(mode=selector.NumberSelectorMode.BOX),
    ),
    vol.Coerce(int),
)

OPTIONS_SCHEMA = vol.Schema(
    {
        vol.Required("host"): selector.TextSelector(),
        vol.Required("port", default=5000): NUMBER_SELECTOR,
    }
)

CONFIG_SCHEMA = vol.Schema(
    {
        vol.Required("name"): selector.TextSelector(),
    }
).extend(OPTIONS_SCHEMA.schema)

# SINE_CONFIG_SCHEMA = vol.Schema(
#     {
#         vol.Required("min", default=0): NUMBER_SELECTOR,
#         vol.Required("max", default=100): NUMBER_SELECTOR,
#         vol.Required("period", default=900): NUMBER_SELECTOR,
#     }
# )
# LINEAR_CONFIG_SCHEMA = vol.Schema(
#     {
#         vol.Required("min", default=0): NUMBER_SELECTOR,
#         vol.Required("max", default=100): NUMBER_SELECTOR,
#         vol.Required("segment_length", default=5): NUMBER_SELECTOR,
#     }
# )

DATA_SCHEMA = vol.Schema({vol.Required(CONF_ID): str})


# async def choose_options_step(options: dict[str, Any]) -> str:
#     """Return next step_id for options flow according to entity_type."""
#     return cast(str, options["type"])


CONFIG_FLOW: dict[str, SchemaFlowFormStep | SchemaFlowMenuStep] = {
    "user": SchemaFlowFormStep(CONFIG_SCHEMA),
    # "sine": SchemaFlowFormStep(SINE_CONFIG_SCHEMA),
    # "linear": SchemaFlowFormStep(LINEAR_CONFIG_SCHEMA),
}

OPTIONS_FLOW: dict[str, SchemaFlowFormStep | SchemaFlowMenuStep] = {
    "init": SchemaFlowFormStep(OPTIONS_SCHEMA),
    # "sine": SchemaFlowFormStep(SINE_CONFIG_SCHEMA),
    # "linear": SchemaFlowFormStep(LINEAR_CONFIG_SCHEMA),
}


class ConfigFlowHandler(SchemaConfigFlowHandler, domain=DOMAIN):
    """Handle a config or options flow for fake_sensor."""

    VERSION = 1

    config_flow = CONFIG_FLOW
    options_flow = OPTIONS_FLOW

    def __init__(self, *args, **kwargs) -> None:
        """Initialize fake_sensor config flow."""
        super().__init__(*args, **kwargs)
        self.data: dict[str, Any] = {}

    def async_config_entry_title(self, options: Mapping[str, Any]) -> str:
        """Return config entry title."""
        return cast(str, options["name"]) if "name" in options else ""

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        errors = {}
        if user_input is not None:
            await self.async_set_unique_id(user_input["name"])
            self._abort_if_unique_id_configured()
            return self.async_create_entry(title=user_input["name"], data=user_input)

        return self.async_show_form(
            step_id="user", data_schema=DATA_SCHEMA, errors=errors
        )

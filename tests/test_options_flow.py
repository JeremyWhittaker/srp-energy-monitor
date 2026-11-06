"""Supported options ownership and reload behavior, without a utility login."""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

pytest.importorskip("voluptuous", reason="Home Assistant supplies the options schema library")

COMPONENT = Path(__file__).parents[1] / "custom_components/srp_energy_monitor"


@pytest.fixture
def flow_modules(monkeypatch):
    def module(name, **attributes):
        obj = types.ModuleType(name)
        obj.__dict__.update(attributes)
        monkeypatch.setitem(sys.modules, name, obj)
        return obj

    class Base:
        def __init_subclass__(cls, **_kwargs):
            return super().__init_subclass__()

        def async_show_form(self, **kwargs):
            return {"type": "form", **kwargs}

        def async_create_entry(self, **kwargs):
            return {"type": "create_entry", **kwargs}

    class Options(Base):
        # Matches HA 2026.10.4: framework binds hass/handler; no setter exists.
        @property
        def config_entry(self):
            return self.hass.config_entries.async_get_known_entry(self.handler)

    for name in ("homeassistant", "homeassistant.helpers"):
        module(name)
    module("homeassistant.config_entries", ConfigEntry=object, ConfigFlow=Base,
           ConfigFlowResult=dict, OptionsFlow=Options)
    module("homeassistant.const", CONF_PASSWORD="password", CONF_USERNAME="username",
           Platform=types.SimpleNamespace(SENSOR="sensor"))
    module("homeassistant.core", HomeAssistant=object, callback=lambda func: func)

    def forbidden_session(*_args, **_kwargs):
        raise AssertionError("Options must not create a utility session")

    module("homeassistant.helpers.aiohttp_client", async_create_clientsession=forbidden_session)
    package = module("srp_options_test")
    package.__path__ = [str(COMPONENT)]
    for name in ("config_flow", "const", "srp_api"):
        monkeypatch.delitem(sys.modules, f"srp_options_test.{name}", raising=False)
    flow = importlib.import_module("srp_options_test.config_flow")
    yield flow, module


def bound(flow_modules, options=None):
    module, _make_module = flow_modules
    entry = types.SimpleNamespace(
        entry_id="existing-entry", options=dict(options or {}),
        data={"account_id": "retained", "username": "retained", "password": "retained"})
    handler = module.SRPEnergyMonitorConfigFlow.async_get_options_flow(entry)
    handler.hass = types.SimpleNamespace(config_entries=types.SimpleNamespace(
        async_get_known_entry=lambda entry_id: entry if entry_id == entry.entry_id else None))
    handler.handler = entry.entry_id
    return handler, entry


@pytest.mark.parametrize(("current", "expected"), [({}, "disabled"),
    ({"demand_audit_plan": "E15"}, "E15"), ({"demand_audit_plan": "E16"}, "disabled")])
def test_form_uses_framework_entry_without_mutating_it(flow_modules, current, expected):
    handler, entry = bound(flow_modules, current)
    result = asyncio.run(handler.async_step_init())
    assert result["type"] == "form"
    assert result["data_schema"]({}) == {"demand_audit_plan": expected}
    assert entry.options == current
    with pytest.raises(AttributeError):
        handler.config_entry = entry


@pytest.mark.parametrize("selected", ["disabled", "E15"])
def test_save_preserves_other_options_credentials_and_ids(flow_modules, selected):
    handler, entry = bound(flow_modules, {"unrelated": {"keep": True}, "demand_audit_plan": "E15"})
    result = asyncio.run(handler.async_step_init({"demand_audit_plan": selected,
                                                 "password": "must-not-save"}))
    assert result == {"type": "create_entry", "title": "", "data": {
        "unrelated": {"keep": True}, "demand_audit_plan": selected}}
    assert entry.data == {"account_id": "retained", "username": "retained", "password": "retained"}
    assert entry.entry_id == "existing-entry"
    assert entry.options["demand_audit_plan"] == "E15"  # Framework saves after the result.


@pytest.mark.parametrize("selected", ["E16", "E27", None, True])
def test_unknown_plan_never_saves(flow_modules, selected):
    handler, entry = bound(flow_modules, {"keep": 1})
    result = asyncio.run(handler.async_step_init({"demand_audit_plan": selected}))
    assert result["type"] == "form"
    assert result["errors"] == {"demand_audit_plan": "invalid_plan"}
    assert entry.options == {"keep": 1}


def test_setup_registers_framework_managed_reload_and_unsubscribe(flow_modules, monkeypatch):
    _flow, make_module = flow_modules
    calls = []

    class Coordinator:
        def __init__(self, _hass, entry):
            self.plan = entry.options.get("demand_audit_plan")

        async def async_config_entry_first_refresh(self):
            calls.append("refresh")

    make_module("srp_options_test.coordinator", SRPConfigEntry=object, SRPCoordinator=Coordinator)
    loader = importlib.machinery.SourceFileLoader(
        "srp_options_test.setup", str(COMPONENT / "__init__.py")
    )
    spec = importlib.util.spec_from_loader(loader.name, loader, is_package=False)
    setup = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(setup)

    async def forward(_entry, platforms):
        calls.append(("platforms", platforms))

    async def reload(entry_id):
        calls.append(("reload", entry_id))

    callbacks = []
    unsubscribe = object()
    entry = types.SimpleNamespace(entry_id="kept-entry", options={"demand_audit_plan": "E15"},
        add_update_listener=lambda callback: callbacks.append(callback) or unsubscribe,
        async_on_unload=lambda callback: calls.append(("cleanup", callback)))
    hass = types.SimpleNamespace(config_entries=types.SimpleNamespace(
        async_forward_entry_setups=forward, async_reload=reload))
    assert asyncio.run(setup.async_setup_entry(hass, entry)) is True
    assert entry.runtime_data.plan == "E15"
    assert ("cleanup", unsubscribe) in calls
    assert len(callbacks) == 1
    asyncio.run(callbacks[0](hass, entry))
    assert calls.count(("reload", "kept-entry")) == 1


def test_options_copy_matches_english_translation():
    source = json.loads((COMPONENT / "strings.json").read_text())["options"]
    translated = json.loads((COMPONENT / "translations/en.json").read_text())["options"]
    assert source == translated

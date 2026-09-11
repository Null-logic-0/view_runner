"""Unit tests for the config-to-Playwright-arguments mapping.

No browser is launched here. The option builders are pure functions precisely
so that the fiddly part of the factory can be checked in microseconds, leaving
only process management for the slow integration tests.
"""

from app.browser.factory import context_options, launch_options
from app.proxy.parser import Proxy
from tests.conftest import make_config


def test_headless_flag_is_passed_through() -> None:
    assert launch_options(make_config(browser={"headless": False}))["headless"] is False


def test_launch_timeout_comes_from_config_as_a_float() -> None:
    options = launch_options(make_config(timeouts={"launch_ms": 5000}))
    assert options["timeout"] == 5000.0
    assert isinstance(options["timeout"], float)


def test_the_browser_sandbox_is_not_disabled() -> None:
    """The old project passed --no-sandbox. That must not come back by accident."""
    assert "--no-sandbox" not in str(launch_options(make_config()))


def test_viewport_comes_from_config() -> None:
    options = context_options(make_config(browser={"viewport_width": 640, "viewport_height": 480}))
    assert options["viewport"] == {"width": 640, "height": 480}


def test_locale_comes_from_config() -> None:
    assert context_options(make_config(browser={"locale": "de-DE"}))["locale"] == "de-DE"


def test_user_agent_is_omitted_when_empty_so_the_browser_default_survives() -> None:
    assert "user_agent" not in context_options(make_config())


def test_user_agent_is_set_when_configured() -> None:
    options = context_options(make_config(browser={"user_agent": "browser-automation-lab/0.1.0"}))
    assert options["user_agent"] == "browser-automation-lab/0.1.0"


def test_no_proxy_key_when_no_proxy_is_given() -> None:
    assert "proxy" not in context_options(make_config())


def test_proxy_server_url_is_built_from_the_proxy() -> None:
    options = context_options(make_config(), Proxy(host="1.2.3.4", port=8080, scheme="socks5"))
    assert options["proxy"] == {"server": "socks5://1.2.3.4:8080"}


def test_credentials_are_separate_keys_never_embedded_in_the_url() -> None:
    """A URL carrying credentials ends up in logs; separate keys do not."""
    proxy = Proxy(host="1.2.3.4", port=8080, username="bob", password="hunter2")
    options = context_options(make_config(), proxy)

    assert options["proxy"]["server"] == "http://1.2.3.4:8080"
    assert "hunter2" not in options["proxy"]["server"]
    assert options["proxy"]["username"] == "bob"
    assert options["proxy"]["password"] == "hunter2"


def test_credential_keys_are_absent_when_the_proxy_has_none() -> None:
    options = context_options(make_config(), Proxy(host="1.2.3.4", port=8080))
    assert set(options["proxy"]) == {"server"}

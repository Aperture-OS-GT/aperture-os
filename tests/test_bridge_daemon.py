"""aperture-bridge --backend real: flags, bus auto-detection and the executor it builds."""
import threading

import pytest

from aperture.rig.virtual import VirtualRig
from aperture.transport import TcpTransport
from pi.bridge import daemon, esp32_flash
from pi.bridge.drivers import lgpio_driver
from pi.bridge.drivers.fake import FakeDriver
from pi.bridge.hardware import HardwareExecutor
from pi.bridge.hardware_buses import BusConfig

WAIT_S = 5


@pytest.fixture
def chips(monkeypatch):
    """LgpioDriver replaced by the fake driver; records the gpiochip each build opens."""
    opened = []

    def fake_lgpio_driver(chip):
        opened.append(chip)
        return FakeDriver()

    monkeypatch.setattr(lgpio_driver, "LgpioDriver", fake_lgpio_driver)
    return opened


@pytest.fixture
def devices(monkeypatch, tmp_path):
    """Bus auto-detection looks under tmp_path; returns the paths and a way to create them."""
    paths = {bus: str(tmp_path / bus) for bus in daemon.AUTO_DEVICES}
    monkeypatch.setattr(daemon, "AUTO_DEVICES", paths)

    def create(*buses):
        for bus in buses:
            (tmp_path / bus).touch()

    return paths, create


def test_virtual_stays_the_default_backend():
    args = daemon.parse_args([])
    executor = daemon.build_executor(args)
    assert args.backend == "virtual" and isinstance(executor, VirtualRig)
    assert daemon.describe(args, executor) == "backend=virtual scenario=empty"


def test_real_backend_builds_a_hardware_executor(chips, devices):
    args = daemon.parse_args(["--backend", "real", "--chip", "4", "--name", "rig-01",
                              "--uart", "/dev/ttyAMA0", "--i2c", "off", "--spi", "on",
                              "--deny", "5,6", "--watchdog-s", "2"])
    executor = daemon.build_executor(args)
    try:
        assert isinstance(executor, HardwareExecutor) and chips == [4]
        assert executor.name == "rig-01" and executor.denied == {5, 6}
        assert executor.limits["watchdog_s"] == 2
        assert executor.buses == BusConfig(uart="/dev/ttyAMA0", spi=True, flash=(24, 25))
        assert executor.reserved[24] == executor.reserved[25] == "flash"
        assert daemon.describe(args, executor) == ("backend=real name=rig-01 uart=/dev/ttyAMA0 "
                                                   "i2c=off spi=on flash=boot24,en25")
    finally:
        executor.close()


def test_auto_buses_follow_the_device_nodes(devices):
    paths, create = devices
    args = daemon.parse_args(["--backend", "real"])
    assert daemon.bus_config(args) == BusConfig()  # nothing present: no buses, no flashing
    create("uart", "i2c")
    assert daemon.bus_config(args) == BusConfig(uart=paths["uart"], i2c=True, flash=(24, 25))
    create("spi")
    assert daemon.bus_config(args).spi
    off = daemon.parse_args(["--backend", "real", "--uart", "off", "--i2c", "off", "--spi", "off"])
    assert daemon.bus_config(off) == BusConfig()
    no_flash = daemon.parse_args(["--backend", "real", "--flash-pins", "off"])
    assert daemon.bus_config(no_flash) == BusConfig(uart=paths["uart"], i2c=True, spi=True)
    moved = daemon.parse_args(["--backend", "real", "--flash-pins", "16,26"])
    assert daemon.bus_config(moved).flash == (16, 26)


def test_default_name_comes_from_the_hostname(monkeypatch):
    monkeypatch.setattr(daemon.socket, "gethostname", lambda: "benchpi.local")
    assert daemon.parse_args([]).name == "aperture-benchpi"


@pytest.mark.parametrize("argv", [["--flash-pins", "24"], ["--flash-pins", "24,24"],
                                  ["--flash-pins", "24,99"], ["--flash-pins", "boot,en"],
                                  ["--deny", "x"], ["--deny", "1"], ["--deny", "25"],
                                  ["--i2c", "maybe"], ["--chip", "-1"]])
def test_parse_args_rejects_bad_real_options(argv):
    with pytest.raises(SystemExit):
        daemon.parse_args(["--backend", "real", *argv])


def test_flash_pins_on_an_enabled_bus_exit_cleanly(chips, devices):
    args = daemon.parse_args(["--backend", "real", "--uart", "/dev/serial0", "--spi", "on",
                              "--flash-pins", "8,25"])
    with pytest.raises(SystemExit, match="flash pins"):
        daemon.build_executor(args)
    assert chips == []


def test_missing_lgpio_exits_with_a_message(monkeypatch, devices):
    def missing(chip):
        raise ImportError("No module named 'lgpio'")

    monkeypatch.setattr(lgpio_driver, "LgpioDriver", missing)
    with pytest.raises(SystemExit, match="cannot open gpiochip0: No module named 'lgpio'"):
        daemon.build_executor(daemon.parse_args(["--backend", "real"]))


def test_busy_header_exits_and_closes_the_driver(monkeypatch, devices):
    driver = FakeDriver()
    driver.fail_release = {17}
    monkeypatch.setattr(lgpio_driver, "LgpioDriver", lambda chip: driver)
    with pytest.raises(SystemExit, match="cannot claim the header GPIOs"):
        daemon.build_executor(daemon.parse_args(["--backend", "real"]))
    assert driver.closed


def test_real_backend_flashes_over_tcp(chips, devices, monkeypatch):
    calls = []

    def fake_flash(driver, images, **kwargs):
        calls.append([(offset, path.read_bytes()) for offset, path in images])
        return {"flashed": True, "seconds": 0.1, "log": "Leaving...\n"}

    monkeypatch.setattr(esp32_flash, "flash", fake_flash)
    server, bridge = daemon.build(daemon.parse_args(["--backend", "real", "--port", "0",
                                                     "--uart", "/dev/serial0"]))
    thread = threading.Thread(target=daemon.serve, args=(server, bridge))
    thread.start()
    transport = TcpTransport("127.0.0.1", server.server_address[1], client="alice")
    try:
        transport.connect()
        assert transport.take_control().ok
        assert transport.request("uart.open", tx=14, rx=15, baud=115200).ok
        assert transport.request("flash.begin").ok
        assert transport.request("flash.write", offset=0x10000, data=b"\xe9app").result == {
            "offset": 0x10000, "size": 4}
        flashed = transport.request("flash.run", manual_boot=True)
        assert flashed.ok and flashed.result["uart_closed"] == [0]
        assert calls == [[(0x10000, b"\xe9app")]]
    finally:
        transport.disconnect()
        server.shutdown()
        thread.join(WAIT_S)

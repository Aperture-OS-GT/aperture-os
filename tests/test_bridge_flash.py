"""flash.* on the hardware executor and the esp32_flash sequence, with fake GPIO and esptool."""
import errno
import functools
import itertools
import subprocess
from pathlib import Path

import pytest

from aperture.protocol import ProtocolError
from aperture.transport.base import DEFAULT_TIMEOUTS
from pi.bridge import esp32_flash
from pi.bridge.drivers.fake import FakeDriver
from pi.bridge.hardware import HardwareExecutor
from pi.bridge.hardware_buses import BusConfig

IDS = itertools.count(1)
APP = 0x10000
FLASHING = BusConfig(uart="/dev/serial0", flash=(24, 25))
ESPTOOL_LOG = "Connecting....\n" + "Writing at 0x00010000... (50 %)\n" * 40 + "Leaving...\n"
RELEASED = [("set_mode", 24, "disabled", 0), ("set_mode", 25, "disabled", 0)]


def run(rig, op, **args):
    try:
        return {"ok": True, **rig.execute({"v": 1, "id": next(IDS), "op": op, **args})}
    except ProtocolError as exc:
        return {"ok": False, "err": exc.code, "msg": exc.msg, **exc.extra}


class FakeFlasher:
    """Stand-in for ``esp32_flash.flash``: records each call with the image bytes it was given."""

    def __init__(self):
        self.calls, self.error = [], None

    def __call__(self, driver, images, port, baud, boot_pin, en_pin, timeout_s):
        self.calls.append({"images": [(offset, path, path.read_bytes()) for offset, path in images],
                           "port": port, "baud": baud, "boot_pin": boot_pin, "en_pin": en_pin,
                           "timeout_s": timeout_s})
        if self.error:
            raise self.error
        return {"flashed": True, "seconds": 1.5, "baud": baud, "images": [], "log": ESPTOOL_LOG}

    def paths(self):
        return [path for call in self.calls for _, path, _ in call["images"]]


@pytest.fixture
def flasher(monkeypatch):
    fake = FakeFlasher()
    monkeypatch.setattr(esp32_flash, "flash", fake)
    return fake


@pytest.fixture
def fast(monkeypatch):
    for name in ("RESET_LOW_S", "BOOT_SETTLE_S", "STRAP_SETTLE_S"):
        monkeypatch.setattr(esp32_flash, name, 0)


@pytest.fixture
def make():
    made = []

    def build(buses=FLASHING):
        driver = FakeDriver()
        rig = HardwareExecutor(driver, "rig-test", buses)
        made.append(rig)
        run(rig, "hello", client="alice", proto=[1])
        assert run(rig, "control.take")["ok"]
        return rig, driver

    yield build
    for rig in made:
        rig.close()


def esptool(driver, returncode=0, output="Hash of data verified.\nLeaving...\n"):
    """Stand-in for ``subprocess.run``: logs the command into the driver's call list."""
    def fake_run(command, **kwargs):
        driver.calls.append(("esptool", command, kwargs["timeout"]))
        return subprocess.CompletedProcess(command, returncode, stdout=output, stderr="")
    return fake_run


def image(folder: Path, name: str = "firmware.bin", data: bytes = b"\xe9app") -> Path:
    path = folder / name
    path.write_bytes(data)
    return path


# --- flash ops on the executor -------------------------------------------------------------
@pytest.mark.parametrize("buses", [BusConfig(), BusConfig(uart="/dev/serial0"),
                                   BusConfig(flash=(24, 25))])
def test_flash_ops_need_the_uart_and_the_flash_pins(make, buses):
    rig, _ = make(buses)
    assert run(rig, "flash.begin")["err"] == "unsupported"
    assert run(rig, "flash.write", offset=APP, data=b"\xe9")["err"] == "unsupported"
    assert run(rig, "flash.run")["err"] == "unsupported"


def test_flash_ops_need_control():
    rig = HardwareExecutor(FakeDriver(), "rig-test", FLASHING)
    assert run(rig, "flash.begin")["err"] == "not_controller"
    assert run(rig, "flash.write", offset=APP, data=b"\xe9")["err"] == "not_controller"


def test_flash_write_appends_per_offset_and_begin_clears(make, flasher):
    rig, _ = make()
    assert run(rig, "flash.begin") == {"ok": True}
    assert run(rig, "flash.write", offset=0x1000, data=b"boot") == {"ok": True, "offset": 0x1000,
                                                                   "size": 4}
    assert run(rig, "flash.write", offset=0x1000, data=b"loader")["size"] == 10
    assert run(rig, "flash.write", offset=APP, data=b"old")["size"] == 3
    assert run(rig, "flash.begin")["ok"]
    assert run(rig, "flash.write", offset=APP, data=b"new")["size"] == 3
    assert run(rig, "flash.run")["images"] == [{"offset": APP, "size": 3}]
    assert [data for _, _, data in flasher.calls[0]["images"]] == [b"new"]


def test_flash_write_caps_all_staged_images_at_4_mib(make):
    rig, _ = make()
    chunk = bytes(32768)
    for index in range(128):  # 128 x 32 KiB = exactly 4 MiB, split over two images
        assert run(rig, "flash.write", offset=0x1000 if index < 64 else APP, data=chunk)["ok"]
    over = run(rig, "flash.write", offset=0x8000, data=b"\x00")
    assert over["err"] == "range" and "4194304" in over["msg"]
    assert sorted(rig.flash_images) == [0x1000, APP] and len(rig.flash_images[APP]) == 64 * 32768


def test_flash_run_needs_the_app_image_and_a_valid_baud(make, flasher):
    rig, _ = make()
    assert run(rig, "flash.run")["err"] == "bad_request"
    run(rig, "flash.write", offset=0x1000, data=b"boot")
    missing = run(rig, "flash.run")
    assert missing["err"] == "bad_request" and "0x10000" in missing["msg"]
    run(rig, "flash.write", offset=APP, data=b"app")
    assert run(rig, "flash.run", baud=100)["err"] == "range"
    assert flasher.calls == [] and rig.flash_images == {}  # every run consumes the staging


def test_flash_run_closes_the_uart_and_flashes_every_staged_image(make, flasher):
    rig, driver = make()
    assert run(rig, "uart.open", tx=14, rx=15, baud=115200)["port"] == 0
    run(rig, "flash.write", offset=APP, data=b"app-image")
    run(rig, "flash.write", offset=0x1000, data=b"bootloader")
    result = run(rig, "flash.run")
    assert result == {"ok": True, "flashed": True, "seconds": 1.5, "uart_closed": [0],
                      "images": [{"offset": 0x1000, "size": 10}, {"offset": APP, "size": 9}],
                      "log": ESPTOOL_LOG[-400:]}
    (call,) = flasher.calls
    assert [(offset, data) for offset, _, data in call["images"]] == [(0x1000, b"bootloader"),
                                                                      (APP, b"app-image")]
    assert (call["port"], call["baud"], call["boot_pin"], call["en_pin"]) == ("/dev/serial0",
                                                                             115200, 24, 25)
    assert call["timeout_s"] < DEFAULT_TIMEOUTS["flash.run"]  # answer before the host gives up
    assert driver.uarts[0].closed and rig.ports == {}
    assert not any(path.exists() for path in flasher.paths())  # temp dir removed
    assert run(rig, "flash.run")["err"] == "bad_request"  # staging consumed
    assert run(rig, "uart.open", tx=14, rx=15, baud=115200)["ok"]  # the caller reopens


def test_manual_boot_leaves_boot_alone_and_baud_passes_through(make, flasher):
    rig, _ = make()
    run(rig, "flash.write", offset=APP, data=b"app")
    assert run(rig, "flash.run", baud=460800, manual_boot=True)["uart_closed"] == []
    (call,) = flasher.calls
    assert (call["boot_pin"], call["en_pin"], call["baud"]) == (None, 25, 460800)


@pytest.mark.parametrize("failure, code", [
    (RuntimeError("esptool failed (2): " + "Connecting....\n" * 200
                  + "A fatal error occurred: Failed to connect to ESP32: No serial data received."),
     "io"),
    (subprocess.TimeoutExpired(["esptool"], 220), "timeout"),
    (OSError(errno.EIO, "lgpio: GPIO busy"), "io"),
])
def test_flash_failures_map_to_errors_and_the_bridge_keeps_serving(make, flasher, failure, code):
    rig, _ = make()
    flasher.error = failure
    run(rig, "uart.open", tx=14, rx=15, baud=115200)
    run(rig, "flash.write", offset=APP, data=b"app")
    failed = run(rig, "flash.run")
    assert failed["err"] == code and len(failed["msg"]) <= 420 and "\n" not in failed["msg"]
    assert rig.flash_images == {} and rig.ports == {}
    assert not any(path.exists() for path in flasher.paths())
    assert run(rig, "list_pins")["ok"] and run(rig, "uart.open", tx=14, rx=15, baud=115200)["ok"]


def test_esptool_failure_message_keeps_the_start_and_the_verdict(make, flasher):
    rig, _ = make()
    flasher.error = RuntimeError("esptool failed (2): " + "Connecting....\n" * 200
                                 + "Failed to connect to ESP32: No serial data received.")
    run(rig, "flash.write", offset=APP, data=b"app")
    message = run(rig, "flash.run")["msg"]
    assert message.startswith("esptool failed (2): Connecting")
    assert message.endswith("Failed to connect to ESP32: No serial data received.")


def test_flash_pins_are_reserved_only_when_flashing_is_possible(make):
    rig, driver = make()
    assert run(rig, "pin.mode", pin=25, mode="output")["err"] == "pin_mode"
    blocked = run(rig, "stream.set", pins=[24], on=True)
    assert blocked["err"] == "pin_mode" and "reserved for flash" in blocked["msg"]
    pins = {p["pin"]: p for p in run(rig, "list_pins")["pins"]}
    assert pins[24]["reserved"] == pins[25]["reserved"] == "flash" and pins[25]["level"] is None
    run(rig, "release_all")
    assert not any(call[1] in (24, 25) for call in driver.calls)  # never claimed or released
    rig, _ = make(BusConfig(flash=(24, 25)))  # no UART, no flashing: ordinary GPIOs
    assert run(rig, "pin.mode", pin=25, mode="output")["ok"]


def test_losing_control_drops_staged_images(make, flasher):
    rig, _ = make()
    run(rig, "flash.write", offset=APP, data=b"app")
    rig.client_disconnected(rig.controller.token)
    run(rig, "control.take")
    assert run(rig, "flash.run")["err"] == "bad_request" and flasher.calls == []


@pytest.mark.parametrize("buses", [{"uart": "/dev/serial0", "flash": (25, 25)},
                                   {"uart": "/dev/serial0", "flash": (14, 25)},   # UART0 TX
                                   {"uart": "/dev/serial0", "flash": (24, 99)},
                                   {"uart": "/dev/serial0", "flash": (23, 24, 25)},
                                   {"spi": True, "flash": (8, 25)}])              # SPI0 CE0
def test_bus_config_rejects_unusable_flash_pins(buses):
    with pytest.raises(ValueError, match="flash pins"):
        BusConfig(**buses)


def test_flash_run_end_to_end_through_the_real_flasher(make, fast, monkeypatch):
    rig, driver = make()
    real_flash = functools.partial(esp32_flash.flash, run=esptool(driver))
    monkeypatch.setattr(esp32_flash, "flash", real_flash)
    run(rig, "flash.write", offset=APP, data=b"\xe9app")
    result = run(rig, "flash.run")
    assert result["flashed"] and result["images"] == [{"offset": APP, "size": 4}]
    (command,) = [call[1] for call in driver.calls if call[0] == "esptool"]
    assert command[command.index("--port") + 1] == "/dev/serial0" and "0x10000" in command
    assert result["log"].endswith("Leaving...\n") and driver.calls[-2:] == RELEASED
    assert run(rig, "pin.mode", pin=25, mode="output")["err"] == "pin_mode"


# --- esp32_flash.flash ------------------------------------------------------------------------
def test_flash_enters_the_bootloader_writes_and_boots_the_new_image(fast, tmp_path):
    driver = FakeDriver()
    images = [(0x1000, image(tmp_path, "bootloader.bin")), (APP, image(tmp_path))]
    result = esp32_flash.flash(driver, images, "/dev/serial0", run=esptool(driver), timeout_s=12)
    sequence = [call[:1] if call[0] == "esptool" else call for call in driver.calls]
    assert sequence == [("set_mode", 24, "output", 1), ("set_mode", 25, "output", 1),
                        ("write", 24, 0),                      # BOOT low through ...
                        ("write", 25, 0), ("write", 25, 1),    # ... an EN pulse: ROM bootloader
                        ("esptool",),
                        ("write", 24, 1),                      # BOOT high through ...
                        ("write", 25, 0), ("write", 25, 1),    # ... a second pulse: run the app
                        *RELEASED]
    (esptool_call,) = [call for call in driver.calls if call[0] == "esptool"]
    _, command, timeout = esptool_call
    option = {flag: command[command.index(flag) + 1] for flag in ("--port", "--baud", "--before",
                                                                  "--after")}
    assert option == {"--port": "/dev/serial0", "--baud": "115200", "--before": "no-reset",
                      "--after": "no-reset"}
    assert command[command.index("write-flash"):] == ["write-flash", "-z", "0x1000",
                                                       str(images[0][1]), "0x10000",
                                                       str(images[1][1])]
    assert timeout == 12 and result["flashed"] and result["log"].endswith("Leaving...\n")


def test_manual_boot_never_touches_boot(fast, tmp_path):
    driver = FakeDriver()
    esp32_flash.flash(driver, [(APP, image(tmp_path))], "/dev/serial0", boot_pin=None,
                      run=esptool(driver))
    assert all(call[1] != 24 for call in driver.calls if call[0] != "esptool")
    assert [call for call in driver.calls if call[0] == "write"] == [("write", 25, 0),
                                                                    ("write", 25, 1)] * 2
    assert driver.calls[-1] == ("set_mode", 25, "disabled", 0)


@pytest.mark.parametrize("hang", [False, True])
def test_esptool_failure_still_releases_both_pins(fast, tmp_path, hang):
    driver = FakeDriver()

    def failing_run(command, **kwargs):
        driver.calls.append(("esptool",))
        if hang:
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        return subprocess.CompletedProcess(command, 2, stdout="",
                                           stderr="A fatal error occurred: Failed to connect")

    expected = subprocess.TimeoutExpired if hang else RuntimeError
    with pytest.raises(expected):
        esp32_flash.flash(driver, [(APP, image(tmp_path))], "/dev/serial0", run=failing_run)
    assert driver.calls[-2:] == RELEASED
    assert ("write", 24, 1) not in driver.calls  # no second reset into a half-written image


def test_esptool_failure_carries_its_output(fast, tmp_path):
    driver = FakeDriver()
    with pytest.raises(RuntimeError, match=r"esptool failed \(2\): .*Failed to connect"):
        esp32_flash.flash(driver, [(APP, image(tmp_path))], "/dev/serial0",
                          run=esptool(driver, 2, "A fatal error occurred: Failed to connect"))


def test_a_pin_that_cannot_be_claimed_is_still_released(fast, tmp_path, monkeypatch):
    driver = FakeDriver()
    claim = driver.set_mode

    def set_mode(pin, mode, level=0):
        if (pin, mode) == (25, "output"):
            raise OSError(errno.EBUSY, "GPIO busy")
        claim(pin, mode, level)

    monkeypatch.setattr(driver, "set_mode", set_mode)
    with pytest.raises(OSError):
        esp32_flash.flash(driver, [(APP, image(tmp_path))], "/dev/serial0", run=esptool(driver))
    assert driver.calls[-2:] == RELEASED and not any(c[0] == "esptool" for c in driver.calls)

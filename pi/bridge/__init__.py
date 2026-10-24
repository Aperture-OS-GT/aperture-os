"""Aperture Pi bridge: protocol v1 over BLE GATT (production) or TCP (bench/dev).

The bridge owns the header, executes requests, streams pin events and fails safe
(every output back to high-impedance) on disconnect or watchdog timeout.
"""

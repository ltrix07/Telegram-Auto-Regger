"""Deprecated Appium-based emulator module.

The project is migrating to ADB + uiautomator2.
Use auto_reger.device_controller.DeviceController instead.
"""


class Emulator:
    def __init__(self, *args, **kwargs):
        raise RuntimeError(
            "auto_reger.emulator is deprecated. "
            "Use auto_reger.device_controller.DeviceController instead."
        )


class Telegram(Emulator):
    pass


class Instagram(Emulator):
    pass


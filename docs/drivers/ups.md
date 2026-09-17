# UPS

APC unit, read over USB HID. A power event is one of the few things that can
end a run, so mains loss goes to SMS at once rather than waiting for the
battery to run down.

!!! warning "Not written yet"
    Wanted: the HID report map, what `ups_on_battery` means, and the runtime estimate's reliability. Source: `src/xams_sc/devices/ups.py`.

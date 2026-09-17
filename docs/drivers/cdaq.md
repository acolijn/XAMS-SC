# NI cDAQ-9174

One chassis, four modules: NI 9207 (voltage), two NI 9216 (PT100), NI 9226
(PT1000).

RTD excitation current is not the DAQmx default and each module accepts
exactly one value — 100 µA for the 9226, 1 mA for the 9216 — so both must be
set explicitly or the task refuses to configure.

!!! warning "Not written yet"
    Wanted: wiring, slot map, the unused 9207 current inputs, and what a module fault looks like from the outside. Source: `src/xams_sc/devices/cdaq.py`, `config/devices.yaml`.

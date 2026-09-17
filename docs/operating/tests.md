# Tests

```powershell
.\.venv\Scripts\python.exe -m pytest
```

83 tests, no hardware and no broker required: the scaling conventions,
configuration validation, bus fan-out, and the chain from a simulated read to a
record on disk.

Several exist because something already went wrong once — the order of
operations in the scaling formula, the sign on HV channels, the
minutes-versus-seconds trap in the flow integrator, and two consumers
subscribing to one MQTT topic.

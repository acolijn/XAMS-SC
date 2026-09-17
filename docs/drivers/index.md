# How a driver works

Every driver does the same four things: find its instrument, prove it is the
right one, read it on a schedule, and publish. None of them write.

Device resolution **never uses a COM number**. Candidates are narrowed by USB
hardware ID, then the instrument is asked who it is.

!!! warning "Not written yet"
    Wanted: the common base class, the heartbeat contract, the failure policy — what a driver does when its instrument stops answering — and why a frozen plausible value is worse than a gap.

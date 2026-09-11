"""The lab bench: a target and proxies you control.

Nothing in `app/` imports anything here. The bench is the instrument, not part
of the system under test -- keeping that boundary means the app can never
accidentally depend on behaviour that only exists in the lab.
"""

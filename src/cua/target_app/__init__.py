"""Mock target application: a legacy-styled credit-union servicing console.

Standalone, self-contained Flask app used as the automation target for the
discovery agent and replay engine. See ``app.py`` for routes/flows and
``faults.py`` for the deterministic fault-injection mechanism.

Run it with ``python -m cua.target_app`` (see that module's docstring for env
vars, or the project README for the full demo path).
"""

"""AI-BEAR: a regime-gated bearish program for the AI-bubble names (AI-BEAR-001).

Research and read-only monitoring only. Nothing in this package places,
queues or approves an order. The owner approves entries through the desk's
supervised path, and live trading is not authorized.

Modules:

* ``params``        the sealed AI-BEAR-001 parameters (digest-checked)
* ``regime_engine`` pure daily regime states: BULL / TOPPING / BEAR / RELIEF /
                    CAPITULATION, from closes and VIX only (no future data)
* ``rules``         the three sleeves' entry and exit rules, shared by the
                    backtest and the monitor
* ``pricing``       synthetic Black-Scholes spread pricing with a declared skew
* ``history``       free daily history (FRED, Yahoo chart) into the desk store
* ``backtest``      the pre-registered AI-BEAR-001 scorer
* ``monitor``       the daily read-only regime, shadow-card and LLM read
"""

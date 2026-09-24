"""Options desk: data, signals and (later) deal mining for trex.

Wave 0 ships two jobs, both systemd user timers (``deploy/desk/``):

* ``record-chains`` (D1 core): the CBOE delayed full option chain for the
  35 optionable panel names, recorded once per session into a columnar
  store (:mod:`tree_options.desk.store`);
* ``eod-equity`` (D0/D5): the research panel refresh plus the two
  surviving direction signals, XSMOM-TOP3 and PEAD beats
  (:mod:`tree_options.desk.signals`), draft cards and an ntfy push.

Wave 1 adds two more:

* ``record-indices`` (D1): the CBOE index histories and FRED DTB3
  (:mod:`tree_options.desk.indices`), revision-safe;
* ``update-events`` (D3): earnings timing and the sealed macro calendar's
  check (:mod:`tree_options.desk.events`; ``seal-macro`` reseals it).

Wave 2 adds ``record-dividends`` (the ex-dividend rail's input,
:mod:`tree_options.desk.dividends`) and the one admission check,
:mod:`tree_options.desk.rails` (with :mod:`~tree_options.desk.book` and
:mod:`~tree_options.desk.beta` feeding it).

Paths come from :mod:`tree_options.desk.paths` (env-overridable; tests pin
every one of them to tmp). Nothing here places orders or seals cards.
"""

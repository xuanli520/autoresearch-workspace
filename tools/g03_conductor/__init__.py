"""g03-conductor: deterministic, ledger-driven G03 exploration controller (see README.md)."""
from .engine import Core, G03Error, Proposals, init, write_control
from .gates import Gates
from .runs import Runs
from .stages import Stages
from .views import Views


class Conductor(Stages, Gates, Runs, Proposals, Views, Core):
    """All behavior composes over the single append-only ledger in <root>/g03/ledger.jsonl."""

    def __init__(self, root, *, client_factory=None):
        Core.__init__(self, root)
        self.client_factory = client_factory


__all__ = ["Conductor", "G03Error", "init", "write_control"]

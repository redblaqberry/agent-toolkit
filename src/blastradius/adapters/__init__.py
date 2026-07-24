"""State-store adapters.

Each adapter parses or captures one kind of store and hands the engine a
NormalizedSnapshot. Import the one you need directly, so optional dependencies
stay optional:

    from blastradius.adapters.snapshot import SnapshotAdapter   # generic JSON, no extra deps
    from blastradius.adapters.silobench import SilobenchAdapter  # SiloBench snapshot.v1
    from blastradius.adapters.sql import SqlAdapter              # needs the `sql` extra (sqlalchemy)

They are not imported here eagerly, so `import blastradius` never pulls in
SQLAlchemy.
"""

from __future__ import annotations

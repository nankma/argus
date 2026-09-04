"""
SqliteStorage -- the default backend, and the base every other backend
inherits from (see storage/postgres/__init__.py). Assembled from the
per-domain mixins in this package via ordinary Python multiple
inheritance: each mixin is a plain class in its own file, and this class
is the only place they're combined. Callers never touch a mixin directly.
"""

from storage.sqlite._primitives import PrimitivesMixin
from storage.sqlite.api_budget import ApiBudgetMixin
from storage.sqlite.category import CategoryMixin
from storage.sqlite.interest_cache import InterestCacheMixin
from storage.sqlite.push_outcome import PushOutcomeMixin
from storage.sqlite.source_state import SourceStateMixin
from storage.sqlite.subscriber import SubscriberMixin


class SqliteStorage(
    PrimitivesMixin,
    SubscriberMixin,
    CategoryMixin,
    PushOutcomeMixin,
    ApiBudgetMixin,
    InterestCacheMixin,
    SourceStateMixin,
):
    def __init__(self, engine):
        self._engine = engine

    def init_db(self) -> None:
        """Schema-only startup: create tables, then apply additive-column
        and api_budget-shape migrations. Seeding the taxonomy's actual
        content is category_ops.bootstrap()'s job, called separately right
        after this -- this class doesn't know what a category IS, only how
        to store one."""
        self.create_schema()
        self.ensure_columns()
        self.migrate_api_budget_table()

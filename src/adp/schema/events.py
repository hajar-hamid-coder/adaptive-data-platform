"""Bus d'événements interne, synchrone et en mémoire (P2-05).

`SchemaChanged` est publié une fois par changement de schéma effectif (jamais pour un schéma
déjà connu, voir `adp.schema.report.report_evolution`). Pas de thread, pas de dépendance.

Un abonné qui lève une exception n'empêche ni les abonnés suivants ni l'émetteur : l'erreur est
journalisée avec son seul type (jamais le message, qui peut contenir une valeur de donnée).
"""

from collections.abc import Callable

from pydantic import BaseModel, ConfigDict, Field

from adp.logging import get_logger
from adp.schema.policy import ChangeImpact, EvolutionAction

log = get_logger(__name__)


class SchemaChanged(BaseModel):
    """Changement de schéma d'une source : versions, semver, impact et nombre de changements."""

    model_config = ConfigDict(frozen=True)

    source_name: str
    from_version: int | None
    to_version: int
    semver: str
    overall_impact: ChangeImpact
    overall_action: EvolutionAction
    change_count: int = Field(ge=0)
    run_id: str | None = None


SchemaSubscriber = Callable[[SchemaChanged], None]


class EventBus:
    """Diffuse chaque événement à tous les abonnés, dans l'ordre d'abonnement."""

    def __init__(self) -> None:
        self._subscribers: list[SchemaSubscriber] = []

    def subscribe(self, callback: SchemaSubscriber) -> None:
        """Ajoute un abonné ; il reçoit tous les événements publiés ensuite."""
        self._subscribers.append(callback)

    def publish(self, event: SchemaChanged) -> None:
        """Appelle chaque abonné ; une erreur d'abonné est journalisée et ignorée."""
        for callback in tuple(self._subscribers):
            try:
                callback(event)
            except Exception as exc:  # isolation : un abonné défaillant ne bloque pas les autres
                log.error(
                    "event_subscriber_failed",
                    bus_event="schema_changed",
                    subscriber=getattr(callback, "__qualname__", "unknown"),
                    error_type=type(exc).__name__,
                    source_name=event.source_name,
                    run_id=event.run_id,
                )

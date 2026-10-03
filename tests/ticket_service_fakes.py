"""Dobles de base de datos compartidos por los tests unitarios de tickets."""

from uuid import uuid4

from app.models.category import CategoryTeam, TicketCategory
from app.models.team import Team, TeamLead, TeamMember
from app.models.ticket import Ticket, TicketDependency
from app.models.user import User


class FakeDb:
    """
    Simula una sesion de base de datos.

    No usamos PostgreSQL real en este test. Solo necesitamos comprobar que
    el service llama a db.add(), db.commit() y db.refresh().
    """

    def __init__(self):
        # Guarda los objetos que el service intenta agregar a la base.
        self.added = []

        # Permite saber si el service confirmo la transaccion.
        self.committed = False

        # Permite saber que objeto se refresco despues del commit.
        self.refreshed = None

        # Permite saber si el service limpio la sesion luego de un error.
        self.rolled_back = False

        # Permite verificar flujos donde necesitamos INSERT antes del commit.
        self.flushed = False

        # Guarda los objetos que el service intenta eliminar.
        self.deleted = []

    def add(self, obj):
        self.added.append(obj)

    def commit(self):
        self.committed = True

    def refresh(self, obj):
        self.refreshed = obj

    def rollback(self):
        self.rolled_back = True

    def flush(self):
        self.flushed = True
        # Simula que SQLAlchemy/base asignan el UUID al hacer INSERT.
        for obj in self.added:
            if isinstance(obj, Ticket) and getattr(obj, "id", None) is None:
                obj.id = uuid4()

    def delete(self, obj):
        self.deleted.append(obj)


class FailingCommitDb(FakeDb):
    def commit(self):
        raise RuntimeError("Commit failed")


class FakeQuery:
    def __init__(self, item=None, items=None, on_for_update=None):
        self.item = item
        self.items = items if items is not None else ([] if item is None else [item])
        self.on_for_update = on_for_update

    def filter(self, *args, **kwargs):
        return self

    def join(self, *args, **kwargs):
        return self

    def with_for_update(self, *args, **kwargs):
        """Imita SELECT FOR UPDATE; el bloqueo real se prueba con PostgreSQL."""

        if self.on_for_update is not None:
            self.on_for_update()
        return self

    def order_by(self, *args, **kwargs):
        return self

    def first(self):
        if isinstance(self.item, list):
            return self.item[0] if self.item else None
        return self.item

    def all(self):
        return self.items


class TeamAwareFakeDb(FakeDb):
    def __init__(
        self,
        ticket=None,
        assigned_user=None,
        team=None,
        member=None,
        category=None,
        category_team=None,
        team_lead=None,
        dependency=None,
        dependencies=None,
        depends_on_ticket=None,
        missing_depends_on_ticket=False,
        reverse_dependency=None,
        open_dependency=None,
        blocked_tickets=None,
        tickets=None,
    ):
        super().__init__()
        self.ticket = ticket
        self.assigned_user = assigned_user
        self.team = team
        self.member = member
        self.category = category
        self.category_team = category_team
        self.team_lead = team_lead
        self.dependency = dependency
        self.dependencies = dependencies
        self.depends_on_ticket = depends_on_ticket
        self.missing_depends_on_ticket = missing_depends_on_ticket
        self.reverse_dependency = reverse_dependency
        self.open_dependency = open_dependency
        self.blocked_tickets = blocked_tickets
        self.tickets = tickets
        self.ticket_query_count = 0
        self.ticket_for_update_count = 0
        self.dependency_id_query_count = 0

    def _record_ticket_for_update(self):
        self.ticket_for_update_count += 1

    def query(self, model):
        model_class = getattr(model, "class_", None)
        if model is TicketDependency:
            first_dependency = self.dependencies[0] if self.dependencies else None
            return FakeQuery(item=first_dependency, items=self.dependencies or [])
        if model_class is TicketDependency:
            self.dependency_id_query_count += 1
            if self.dependency_id_query_count == 1:
                return FakeQuery(self.open_dependency or self.dependency)
            if self.dependency_id_query_count == 2:
                return FakeQuery(self.reverse_dependency)
            return FakeQuery(None)
        if model is CategoryTeam or model_class is CategoryTeam:
            return FakeQuery(self.category_team)
        if model is TicketCategory or model_class is TicketCategory:
            return FakeQuery(self.category)
        if model is Ticket or model_class is Ticket:
            self.ticket_query_count += 1
            if self.tickets is not None:
                return FakeQuery(item=None, items=self.tickets)
            if self.ticket_query_count == 2 and self.blocked_tickets is not None:
                return FakeQuery(item=None, items=self.blocked_tickets)
            if self.ticket_query_count == 2 and self.missing_depends_on_ticket:
                return FakeQuery(None)
            if self.ticket_query_count == 2 and self.depends_on_ticket is not None:
                return FakeQuery(self.depends_on_ticket)
            return FakeQuery(self.ticket, on_for_update=self._record_ticket_for_update)
        if model is User or model_class is User:
            return FakeQuery(self.assigned_user)
        if model is Team or model_class is Team:
            return FakeQuery(self.team)
        if model is TeamLead or model_class is TeamLead:
            return FakeQuery(self.team_lead)
        if model is TeamMember or model_class is TeamMember:
            return FakeQuery(self.member)
        raise AssertionError(f"Unexpected model queried: {model}")


class FailingTeamAwareFakeDb(TeamAwareFakeDb):
    def commit(self):
        raise RuntimeError("Commit failed")

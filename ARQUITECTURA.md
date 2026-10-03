# Arquitectura y decisiones técnicas del sistema de tickets

Este documento describe el estado actual del backend, explica cómo se distribuyen
las responsabilidades y registra las principales decisiones de diseño. Sirve como
guía de estudio y como base para defender técnicamente el proyecto.

## 1. Resumen ejecutivo

El proyecto es una API REST construida con FastAPI para administrar tickets de
soporte. Usa PostgreSQL como base de datos, SQLAlchemy como ORM, Alembic para
migraciones, Pydantic para validar contratos HTTP y JWT para autenticación.

La arquitectura separa:

```text
Request HTTP
    -> Router de FastAPI
    -> Dependencias de autenticación
    -> Service del caso de uso
    -> Reglas de negocio puras y consultas auxiliares
    -> Modelos SQLAlchemy
    -> PostgreSQL
```

La idea central es que el router traduzca HTTP, mientras que el service ejecuta
el caso de uso completo. Esto permite reutilizar la lógica desde endpoints,
scripts o futuros workers sin depender de FastAPI.

### Orden recomendado de lectura

Para estudiar un flujo sin saltar entre archivos al azar:

```text
Schema de entrada/salida
    -> endpoint del router
    -> dependencia de autenticación
    -> service invocado
    -> regla pura o query auxiliar
    -> modelo e historial afectados
    -> test de regla
    -> test de service
    -> test de endpoint
    -> test de integración
```

Para un primer recorrido completo conviene seguir: autenticación, creación de
ticket, visibilidad, asignación a team, asignación a persona, estados,
comentarios, dependencias y archivado.

## 2. Conceptos del dominio

### Usuarios y roles

- `USER`: solicitante que crea el ticket porque necesita asistencia.
- `AGENT`: usuario operativo que atiende tickets.
- `ADMIN`: administrador global. Puede gestionar usuarios, categorías, equipos y
  tickets; además puede ser miembro o lead de un equipo y recibir tickets.
- `TeamLead`: no es un rol global. Es una responsabilidad que un `AGENT` o
  `ADMIN` activo posee dentro de un equipo concreto.

Separar `TeamLead` de `UserRole` evita crear roles globales como
`TEAM_LEAD`. Una misma persona puede liderar un equipo, ser miembro de otro y no
tener autoridad sobre los demás.

### Categorías, equipos y responsables

```text
Categoría -> define qué tipo de problema representa el ticket
Equipo    -> grupo responsable de atenderlo
Asignado  -> persona responsable dentro del equipo
```

El flujo actual exige una categoría al crear el ticket, pero permite que nazca
sin equipo y sin responsable:

```text
category_id != NULL
team_id      = NULL
assigned_to  = NULL
```

Para asignarlo a una persona primero debe ingresar a un equipo y esa persona
debe ser miembro del mismo. La base refuerza una parte de esta regla mediante:

```text
assigned_to IS NULL OR team_id IS NOT NULL
```

La pertenencia exacta del responsable al equipo se valida en el service porque
involucra otra tabla (`team_members`) y no puede expresarse con un `CHECK`
sencillo.

### Estados

```text
OPEN -> IN_PROGRESS | ON_HOLD | RESOLVED
IN_PROGRESS -> ON_HOLD | RESOLVED
ON_HOLD -> IN_PROGRESS | RESOLVED
RESOLVED -> OPEN | CLOSED
CLOSED -> sin transiciones
```

Reglas adicionales:

- Un `USER` no cambia estados.
- Un `AGENT` solo cambia el estado de un ticket asignado directamente a él.
- Solo `ADMIN` puede llevar un ticket a `CLOSED`.
- No se puede resolver un ticket con dependencias activas todavía abiertas.
- Pasar a `ON_HOLD`, cerrar o reabrir un resuelto exige un motivo.

### Archivado

El archivado es independiente del estado. Un ticket permanece `CLOSED`, pero
deja de aparecer en los listados operativos:

```text
status = CLOSED
archived_at != NULL
```

Solo un administrador puede archivar o desarchivar. Desarchivar vuelve a mostrar
el ticket, pero no lo reabre. También existe un service y un script para archivar
automáticamente tickets cerrados antiguos.

### Comentarios

Los comentarios tienen visibilidad explícita:

- `REQUESTER_VISIBLE`: respuesta visible para el solicitante.
- `INTERNAL`: nota operativa que el solicitante no puede leer.

El solicitante solo comenta en su propio ticket y siempre de forma pública. Un
miembro del equipo puede escribir notas internas; solamente el responsable
directo o un lead puede responder al solicitante. `ADMIN` tiene alcance global.
Los tickets cerrados o archivados conservan sus comentarios, pero no reciben
nuevos.

No existe edición de comentarios en la versión actual. Esto evita alterar la
auditoría sin haber diseñado todavía campos como `edited_at` o historial de
ediciones.

### Dependencias

Si `A` depende de `B`, `A` no puede resolverse mientras `B` siga abierto:

```text
A.ticket_id -> ticket bloqueado
B.depends_on_ticket_id -> ticket bloqueante
```

Se impiden dependencias consigo mismo, duplicados activos y ciclos directos
`A -> B -> A`. Al remover una dependencia se aplica soft delete: deja de bloquear,
pero conserva quién la removió, cuándo y por qué.

También puede crearse un nuevo ticket bloqueante. El service hace `flush()` para
obtener su UUID sin confirmar la transacción y luego crea la dependencia. Ticket,
dependencia y cambio a `ON_HOLD` se confirman juntos; si algo falla, se revierte
todo.

## 3. Estructura por capas

### `app/main.py`

Punto de entrada de FastAPI. Crea la aplicación y registra los routers. No usa
`Base.metadata.create_all()`: el esquema se administra exclusivamente mediante
Alembic para que cada cambio quede versionado y pueda aplicarse o revertirse.

### `app/core/`

#### `config.py`

Centraliza configuración con `pydantic-settings`. Lee `.env` y variables con
prefijo `APP_`, como:

- conexión de desarrollo y de tests;
- secreto, algoritmo y vencimiento de JWT;
- antigüedad para archivado automático.

Esto evita credenciales hardcodeadas y permite variar la configuración según el
entorno sin modificar código.

#### `security.py`

Contiene primitivas de seguridad:

- normalización de email;
- hash y verificación de contraseñas con bcrypt;
- validación del límite de 72 bytes de bcrypt;
- creación y decodificación de JWT;
- claims obligatorios `sub`, `iat` y `exp`.

El token actual es un access token firmado con HS256. No hay todavía refresh
tokens, revocación, `jti` ni logout del lado del servidor.

#### `ticket_rules.py`

Reúne reglas puras que responden preguntas booleanas, por ejemplo:

- si una transición de estado es válida;
- si un usuario puede ver o asignar un ticket;
- si puede consultar historiales;
- si puede reclamar un ticket;
- qué tipo de comentario puede publicar.

Estas funciones no hacen queries ni commits. Reciben el contexto ya calculado,
como `is_team_member`, lo cual permite probar todas las combinaciones sin una
base de datos.

### `app/api/`

#### `deps.py`

Implementa la cadena de autenticación:

```text
OAuth2PasswordBearer
    -> get_current_user
       decodifica JWT, toma sub y busca User
    -> get_current_active_user
       rechaza usuarios inactivos
    -> require_roles(...)
       restringe el endpoint por rol global
```

`require_roles()` devuelve otra función porque FastAPI necesita recibir una
dependencia configurable. La autorización sobre un recurso concreto sigue en el
service; tener rol `AGENT` no significa poder modificar cualquier ticket.

#### `routes/auth.py`

Expone `POST /auth/login`. El campo OAuth2 `username` se interpreta como email,
se normaliza, se compara la contraseña y se verifica que el usuario esté activo
antes de emitir el token.

#### `routes/users.py`

- lista usuarios para administradores;
- devuelve el usuario autenticado;
- permite consultar el perfil propio o cualquier perfil siendo admin;
- crea usuarios mediante `user_service`.

#### `routes/categories.py`

- crea y lista categorías;
- permite a admin incluir categorías inactivas;
- asocia equipos con categorías;
- expone la cola de tickets todavía sin equipo dentro de una categoría.

#### `routes/teams.py`

- crea y consulta equipos;
- administra miembros y múltiples leads;
- impide quitar al último lead;
- configura reclamo manual y autoasignación por delay;
- lista la cola de tickets de un equipo.

Algunas lecturas simples todavía consultan SQLAlchemy directamente en este
router. Los casos de uso con invariantes y transacciones sí viven en services.

#### `routes/tickets.py`

Es el router más amplio y está dividido por secciones:

- creación y listados;
- archivado;
- estado, categoría, equipo y responsable;
- reclamo voluntario;
- dependencias;
- comentarios;
- historiales.

El helper `_ticket_service_error_to_http()` traduce excepciones del dominio:

```text
No encontrado -> 404
Sin permiso   -> 403
Regla inválida -> 400
```

El service no conoce códigos HTTP, por lo que puede reutilizarse fuera de una
request web.

### `app/schemas/`

Los schemas Pydantic definen el contrato externo de la API:

- `auth.py`: token emitido y payload JWT tipado.
- `user.py`: datos permitidos al crear y devolver usuarios.
- `category.py`: creación, lectura y asociación categoría-equipo.
- `team.py`: creación, miembros, leads y configuración de asignación.
- `ticket.py`: creación, lectura, cambios, comentarios, dependencias e
  historiales.

Se separan schemas de entrada y salida porque el cliente no debe enviar campos
controlados por el sistema. Por ejemplo, `TicketCreate` recibe título,
descripción, categoría y prioridad; `TicketRead` también devuelve UUID, estado,
creador, equipo, responsable y datos de archivado.

`ConfigDict(from_attributes=True)` permite construir respuestas Pydantic desde
objetos SQLAlchemy sin convertirlos manualmente a diccionarios.

### `app/models/`

#### `user.py`

Define `UserRole` y `User`. Almacena identidad, password hasheado, rol, actividad
y timestamps. Sus relaciones permiten navegar tickets creados, tickets asignados,
membresías, liderazgos y comentarios.

#### `category.py`

Define `TicketCategory` y la tabla intermedia `CategoryTeam`. La relación es
muchos a muchos: una categoría puede ser atendida por varios equipos y un equipo
puede atender varias categorías.

Las categorías se desactivan en vez de eliminarse para conservar tickets
históricos. El índice único sobre `lower(name)` evita duplicados como `Hardware`
y `hardware`, incluso ante requests concurrentes.

#### `team.py`

Define:

- `Team`: equipo y sus políticas de asignación;
- `TeamMember`: pertenencia operativa;
- `TeamLead`: responsabilidad de liderazgo;
- `AssignmentStrategy`: criterio previsto para autoasignar.

Un lead también debe ser miembro. La tabla `TeamLead` es la fuente de verdad y
permite varios leads, evitando depender de una única persona disponible.

`self_assignment_enabled` controla si un agente puede reclamar voluntariamente
un ticket del equipo. `auto_assignment_enabled`, el delay y la estrategia
configuran una futura asignación hecha por el sistema.

La configuración se copia al ticket cuando entra en cada cola. Así, un cambio
posterior en la categoría o el team solo afecta tickets futuros. El ejecutor
transaccional ya está implementado; por ahora se invoca mediante un script y
queda pendiente conectarlo a un worker periódico.

#### `ticket.py`

Contiene la entidad principal y sus entidades de auditoría:

- `Ticket`;
- `TicketComment`;
- `TicketStatusHistory`;
- `TicketAssignmentHistory`;
- `TicketTeamHistory`;
- `TicketCategoryHistory`;
- `TicketDependency`.

Se mantienen en `Ticket` los UUID `created_by`, `assigned_to`, `team_id` y
`category_id` porque son las columnas persistidas. Las relaciones ORM agregan
navegación orientada a objetos sin sustituir esas claves.

### `app/services/`

#### `ticket_service.py`

Creación y lecturas principales: valida categoría activa, crea el ticket abierto,
lista tickets propios/asignados/visibles y autoriza el detalle.

#### `ticket_assignment_service.py`

Agrupa cambios de responsabilidad organizativa:

- asignar responsable;
- reclamar ticket usando bloqueo `FOR UPDATE`;
- asignar equipo;
- cambiar categoría;
- consultar historiales de responsable, equipo y categoría.

Cada modificación registra su historial dentro de la misma transacción. Una
recategorización limpia equipo y responsable porque podrían quedar incoherentes
con la nueva categoría.

La asignación manual de responsable, la asignación de equipo y el cambio de
categoría también bloquean la fila con `FOR UPDATE`. Si otra transacción está
modificando ese ticket, esperan a que termine y vuelven a evaluar las reglas
sobre el estado confirmado más reciente. Ante cualquier error se ejecuta
`rollback()` para liberar el lock.

El reclamo está habilitado para usuarios `AGENT` o `ADMIN` activos, miembros del
equipo y cuando este habilitó `self_assignment_enabled`. El alcance global de un
administrador no reemplaza la membresía: para reclamar participa como un miembro
operativo más.

#### `ticket_lifecycle_service.py`

Gestiona estados, historial de estado, archivado manual, desarchivado y archivado
por antigüedad. Es responsable de validar dependencias abiertas antes de resolver.

#### `ticket_dependency_service.py`

Crea, consulta y remueve dependencias. También implementa la creación atómica de
un ticket bloqueante. La gestión está permitida para admins o leads del equipo
del ticket.

#### `ticket_comment_service.py`

Valida acceso, visibilidad, estado del ticket y contenido no vacío. En lectura,
filtra notas internas para que el solicitante reciba solamente respuestas
públicas. La paginación usa fecha e ID para mantener un orden estable.

#### `ticket_auto_assignment_service.py`

Procesa las dos etapas de autoasignación. Primero selecciona entre los equipos
asociados a la categoría usando carga activa por miembro operativo. Después
elige un miembro activo mediante `LEAST_ACTIVE` o `LONGEST_IDLE`. Las filas de
ticket se bloquean con `FOR UPDATE SKIP LOCKED` para que dos workers no procesen
el mismo ticket; si no hay candidato, permanece en cola para otro intento.

Cada intento devuelve `ASSIGNED`, `NO_CANDIDATE` o `SKIPPED`. El procesador
aísla las excepciones por ticket, hace rollback, registra el ticket y la etapa,
y continúa con el resto. `AutoAssignmentResult` resume asignaciones, ausencia de
candidatos, omisiones y errores para facilitar el monitoreo del futuro worker.

La configuración central ya define broker y lock en Redis, frecuencia de
consulta, tamaño de tanda por fase y límites de tiempo. La tarea Celery todavía
no forma parte de esta etapa: estos valores preparan su incorporación sin
mezclar infraestructura con el service de dominio.

Los historiales distinguen `MANUAL`, `CLAIM` y `AUTOMATIC`. En una acción
automática, `changed_by` queda en `NULL` porque no intervino una persona.

#### `ticket_exceptions.py`

Define excepciones específicas del dominio. Nombres como
`InvalidStatusTransitionError` o `TicketBlockedByOpenDependenciesError` explican
el fallo mejor que un `ValueError` genérico y permiten que el router traduzca
cada familia al código HTTP correcto.

#### `ticket_service_utils.py`

Contiene utilidades pequeñas compartidas. `commit_and_refresh()` hace rollback si
falla el commit; `normalize_optional_reason()` elimina espacios y convierte texto
vacío en `None`.

#### `team_service.py`

Gestiona creación de equipos, leads, miembros y políticas de asignación. Usa
`flush()` al crear el equipo para obtener su UUID y registrar lead y miembro antes
del único commit.

#### `category_service.py`

Normaliza y crea categorías, asocia equipos y devuelve la cola de tickets sin
equipo. Hace una validación previa de duplicado para responder claramente y deja
el índice único como protección definitiva ante concurrencia.

#### `user_service.py`

Normaliza email, verifica duplicados, hashea la contraseña y persiste el usuario.
También captura `IntegrityError`, porque una validación previa por sí sola no
protege dos inserciones concurrentes.

#### `team_queries.py` y `category_queries.py`

Centralizan consultas booleanas reutilizables sobre membresía, liderazgo y
asociación categoría-equipo. Consultan solo el ID porque únicamente necesitan
saber si la fila existe.

### `app/db/`

- `base.py`: declara la base compartida por todos los modelos SQLAlchemy.
- `session.py`: crea el engine y `SessionLocal`; `get_db()` entrega una sesión por
  request y garantiza su cierre mediante `yield/finally`.

Cerrar la sesión no reemplaza el rollback. El rollback se realiza en el service
cuando falla una transacción; el `finally` de la dependencia libera recursos.

### `app/scripts/`

- `archive_closed_tickets.py`: ejecuta el archivado por antigüedad reutilizando el
  service. Está preparado para que un futuro worker invoque la misma lógica.
- `create_test_database.py`: crea o recrea una base PostgreSQL aislada terminada
  en `_test`, con validaciones para no eliminar accidentalmente desarrollo.

## 4. Flujos principales

### Crear y atender un ticket

```text
USER crea ticket con categoría
    -> Ticket OPEN, sin team y sin responsable
    -> aparece en la cola de la categoría
ADMIN o lead de un team habilitado lo asigna al equipo
    -> aparece en la cola del equipo
ADMIN o TeamLead asigna un miembro
    o AGENT lo reclama si self_assignment_enabled
    -> responsable cambia estado y trabaja el ticket
AGENT lo lleva a RESOLVED
ADMIN lo lleva a CLOSED
ADMIN o proceso programado lo archiva
```

### Autenticación

```text
email + password
    -> normalización de email
    -> búsqueda de User
    -> verify_password()
    -> control de is_active
    -> JWT con sub=user.id, iat y exp
    -> Authorization: Bearer <token>
```

### Cambio con auditoría

```text
Service carga Ticket
    -> valida permiso sobre el recurso
    -> valida regla de negocio
    -> modifica Ticket
    -> agrega History
    -> commit único
    -> refresh

Si falla:
    -> rollback
    -> no queda un cambio parcial
```

## 5. Decisiones técnicas defendibles

### UUID como clave primaria

Permite generar identificadores sin depender de una secuencia central y evita
exponer IDs correlativos. El costo es que ocupa más espacio y es menos cómodo de
leer manualmente que un entero.

### Enums en roles, prioridades y estados

Evitan strings arbitrarios y errores ortográficos, documentan los valores válidos
y mejoran el tipado. Cambiar un enum persistido requiere una migración explícita.

### Services autocontenidos

Los services reciben IDs y buscan sus entidades. De esta forma el router no
necesita conocer el orden de consultas ni las invariantes completas del caso de
uso.

### Revalidación de permisos dentro del service

La dependencia del endpoint valida autenticación y rol general. El service valida
el recurso concreto. Esto evita que una llamada desde un script o un endpoint
nuevo omita accidentalmente reglas críticas.

### Excepciones de dominio

Separan el significado del error de su representación HTTP. El mismo service
podría ser usado por una tarea programada, que no necesita `HTTPException`.

### Historiales separados

Estado, responsable, equipo y categoría tienen tablas distintas. Esto mantiene
tipos y reglas claras, permite permisos diferentes y evita una tabla genérica con
muchas columnas opcionales y payloads difíciles de consultar.

### Soft delete selectivo

Se usa para dependencias removidas y archivado de tickets porque interesa
conservar trazabilidad. No se aplica indiscriminadamente a todas las tablas.

### `flush()` antes del commit

Envía SQL pendiente a la base y obtiene valores generados, pero no confirma la
transacción. Los datos todavía pueden revertirse y normalmente no son visibles
para otras transacciones hasta el commit.

### `FOR UPDATE` al reclamar

Bloquea la fila durante la transacción. Si dos agentes intentan reclamar el mismo
ticket simultáneamente, el segundo espera y luego vuelve a leer el ticket ya
asignado. Esto protege una operación especialmente sensible a concurrencia.

### Alembic en lugar de `create_all()`

`create_all()` crea elementos faltantes, pero no versiona ni transforma esquemas
existentes. Alembic conserva una secuencia reproducible de cambios con
`upgrade()` y `downgrade()`.

### Paginación desde el comienzo

Los listados aceptan `skip` y `limit`, con máximo de 100. Aunque hoy existan pocos
tickets, evita respuestas ilimitadas y mantiene estable el contrato al crecer.

## 6. Migraciones

Las migraciones de `alembic/versions/` cuentan la evolución del modelo:

1. esquema inicial;
2. conversión de roles y prioridades a enums;
3. rol de usuario obligatorio;
4. motivo en cambios de estado;
5. campos obligatorios del historial de estados;
6. equipos de trabajo;
7. categorías y relación muchos a muchos con equipos;
8. nombre de categoría único sin distinguir mayúsculas;
9. historial de equipos;
10. historial de categorías;
11. dependencias entre tickets;
12. soft delete de dependencias;
13. múltiples leads por equipo;
14. eliminación del antiguo `Team.lead_id`;
15. archivado de tickets;
16. comentarios con visibilidad;
17. obligación de tener team antes de responsable;
18. reclamo manual por equipo;
19. autoasignación escalonada de categoría a team y de team a responsable.

Comandos habituales:

```bash
python -m alembic current
python -m alembic revision --autogenerate -m "descripcion clara"
python -m alembic upgrade head
python -m alembic check
```

Siempre se revisa la migración autogenerada antes de aplicarla, especialmente en
renombres, enums y transformaciones de datos.

## 7. Estrategia de tests

La suite se divide por alcance:

```text
Reglas puras
    -> combinaciones booleanas sin FastAPI ni DB
Services unitarios
    -> casos de uso con sesiones y queries simuladas
Rutas con TestClient
    -> contrato HTTP, dependencias, serialización y traducción de errores
Integración
    -> SQLAlchemy + constraints + migraciones + PostgreSQL real de test
```

Archivos principales:

- `tests/test_ticket_rules.py`: matriz de permisos y transiciones.
- `tests/test_ticket_*_service.py`: services divididos por responsabilidad.
- `tests/test_ticket_routes.py`: endpoints de tickets con `TestClient`.
- `tests/test_team_service.py` y `test_team_routes.py`: equipos y configuración.
- `tests/test_auth.py`: login, credenciales y usuarios inactivos.
- `tests/ticket_service_fakes.py`: dobles reutilizables para tests unitarios.
- `tests/integration/`: flujos reales contra PostgreSQL aislado.

`dependency_overrides` sustituye `get_db` y autenticación en tests HTTP. Esto no
reemplaza los tests de integración: permite que cada nivel pruebe una
responsabilidad distinta sin duplicar toda la lógica.

Comando completo:

```bash
python -m pytest -q
```

## 8. Configuración de asignaciones

Hay dos mecanismos diferentes:

### Reclamo manual

```text
self_assignment_enabled = true
```

Un `AGENT` o `ADMIN` miembro puede tomar voluntariamente un ticket `OPEN`, no
archivado, sin responsable y perteneciente a su equipo.

### Autoasignación escalonada

```text
auto_assignment_enabled
auto_assignment_delay_minutes
assignment_strategy = LEAST_ACTIVE | LONGEST_IDLE
```

Estos campos permiten que cada equipo defina su política. El vencimiento se
calcula al ingresar al team y queda guardado en el ticket junto con la
estrategia vigente. El service actualiza responsable e historial en una misma
transacción. Falta conectar ese procesamiento a un worker periódico y ampliar
las pruebas de concurrencia con varios workers reales.

## 9. Deuda técnica y próximos pasos

### Prioridad alta

1. Integrar `process_due_auto_assignments()` con una tarea Celery periódica que
   use el broker, los límites y el lock distribuido ya configurados.
2. Completar el uso de datetimes con timezone. Algunos campos antiguos todavía
   usan `datetime.utcnow()` y generan advertencias.
3. Decidir si las consultas simples que aún viven en routers de teams/categories/
   users deben pasar a services al crecer sus reglas.

### Prioridad media

1. Agregar optimistic locking con `version_id` para ediciones concurrentes más
   generales, no solo reclamos.
2. Diseñar edición de comentarios con `edited_at` o historial si se decide
   permitirla.
3. Incorporar refresh tokens revocables si la primera versión necesita sesiones
   largas y logout real.
4. Ejecutar archivado y notificaciones mediante un worker. Celery es una opción,
   pero conviene introducirlo cuando exista más de una tarea asíncrona real.
5. Usar transactional outbox antes de enviar emails asociados a transacciones
   importantes.

## 10. Guion breve para defender el proyecto

Una explicación de dos minutos puede seguir este orden:

1. **Problema:** administrar tickets con solicitantes, agentes, equipos,
   categorías, estados y trazabilidad.
2. **Arquitectura:** FastAPI para HTTP, services para casos de uso, reglas puras
   para autorización, SQLAlchemy para persistencia y Alembic para evolución.
3. **Consistencia:** transacciones únicas, rollback, constraints, índices únicos,
   `flush()` y bloqueo de fila donde existe riesgo de concurrencia.
4. **Seguridad:** passwords con bcrypt, JWT tipado, usuarios activos, roles
   globales y autorización adicional sobre cada recurso.
5. **Auditoría:** historiales separados, comentarios con visibilidad, soft delete
   de dependencias y archivado independiente del estado.
6. **Calidad:** tests por capas y base PostgreSQL aislada para integración.
7. **Evolución:** la autoasignación ya tiene configuración persistida, pero su
   motor y worker se incorporarán como siguiente etapa sin mezclar esa complejidad
   con los casos de uso actuales.

La defensa más sólida no consiste en afirmar que todo está terminado, sino en
explicar qué problema resuelve cada decisión, qué garantía aporta y qué costo o
trabajo futuro introduce.

## 11. Mapa navegable fichero por fichero

### Arranque y configuración

- [`app/main.py`](app/main.py): crea FastAPI y registra todos los routers.
- [`app/core/config.py`](app/core/config.py): carga configuración desde `.env` y
  variables `APP_*`.
- [`app/core/security.py`](app/core/security.py): bcrypt, normalización de email y
  JWT.
- [`app/core/ticket_rules.py`](app/core/ticket_rules.py): reglas puras de permisos
  y transiciones.
- [`app/db/base.py`](app/db/base.py): `Base` declarativa de SQLAlchemy.
- [`app/db/session.py`](app/db/session.py): engine, fábrica de sesiones y
  dependencia `get_db()`.
- [`requirements.txt`](requirements.txt): dependencias necesarias para ejecutar
  la aplicación.
- [`requirements-dev.txt`](requirements-dev.txt): dependencias adicionales de
  desarrollo y tests.

### API HTTP

- [`app/api/deps.py`](app/api/deps.py): usuario actual, usuario activo y control
  de roles.
- [`app/api/routes/auth.py`](app/api/routes/auth.py): login y emisión de token.
- [`app/api/routes/users.py`](app/api/routes/users.py): creación y consulta de
  usuarios.
- [`app/api/routes/categories.py`](app/api/routes/categories.py): categorías,
  asociaciones y cola sin equipo.
- [`app/api/routes/teams.py`](app/api/routes/teams.py): equipos, miembros, leads,
  configuración y cola del equipo.
- [`app/api/routes/tickets.py`](app/api/routes/tickets.py): todos los endpoints
  operativos del ticket, organizados por secciones.

### Schemas Pydantic

- [`app/schemas/auth.py`](app/schemas/auth.py): respuesta del login y payload JWT.
- [`app/schemas/user.py`](app/schemas/user.py): entrada y salida de usuarios.
- [`app/schemas/category.py`](app/schemas/category.py): categorías y relación con
  equipos.
- [`app/schemas/team.py`](app/schemas/team.py): equipos, miembros, leads y
  políticas de asignación.
- [`app/schemas/ticket.py`](app/schemas/ticket.py): contratos de tickets,
  comentarios, historiales y dependencias.

### Modelos SQLAlchemy

- [`app/models/user.py`](app/models/user.py): usuario, roles y relaciones.
- [`app/models/category.py`](app/models/category.py): categoría y asociación
  muchos a muchos con equipos.
- [`app/models/team.py`](app/models/team.py): equipo, miembros, múltiples leads y
  configuración de asignación.
- [`app/models/ticket.py`](app/models/ticket.py): ticket, comentarios, dependencias
  y tablas de historial.

### Services

- [`app/services/user_service.py`](app/services/user_service.py): creación segura
  de usuarios.
- [`app/services/category_service.py`](app/services/category_service.py): creación
  de categorías, asociaciones y cola.
- [`app/services/category_queries.py`](app/services/category_queries.py): consulta
  de asociación entre categoría y equipo.
- [`app/services/team_service.py`](app/services/team_service.py): administración
  transaccional de equipos, miembros, leads y políticas.
- [`app/services/team_queries.py`](app/services/team_queries.py): consultas de
  membresía y liderazgo.
- [`app/services/ticket_service.py`](app/services/ticket_service.py): creación y
  lecturas principales de tickets.
- [`app/services/ticket_assignment_service.py`](app/services/ticket_assignment_service.py):
  responsable, reclamo, equipo, categoría e historiales organizativos.
- [`app/services/ticket_lifecycle_service.py`](app/services/ticket_lifecycle_service.py):
  estados, historial de estado y archivado.
- [`app/services/ticket_comment_service.py`](app/services/ticket_comment_service.py):
  creación, permisos y lectura filtrada de comentarios.
- [`app/services/ticket_auto_assignment_service.py`](app/services/ticket_auto_assignment_service.py):
  selección y procesamiento transaccional de teams y responsables automáticos.
- [`app/services/assignment_timing.py`](app/services/assignment_timing.py):
  cálculo centralizado de vencimientos según configuración y delay.
- [`app/services/ticket_dependency_service.py`](app/services/ticket_dependency_service.py):
  dependencias y creación de tickets bloqueantes.
- [`app/services/ticket_exceptions.py`](app/services/ticket_exceptions.py):
  excepciones específicas del dominio de tickets.
- [`app/services/ticket_service_utils.py`](app/services/ticket_service_utils.py):
  commit/rollback compartido y normalización de motivos.

### Scripts

- [`app/scripts/archive_closed_tickets.py`](app/scripts/archive_closed_tickets.py):
  ejecuta archivado por antigüedad desde consola.
- [`app/scripts/process_auto_assignments.py`](app/scripts/process_auto_assignments.py):
  procesa manualmente una tanda de autoasignaciones vencidas.
- [`app/scripts/create_test_database.py`](app/scripts/create_test_database.py):
  crea o reconstruye exclusivamente la base de integración.


### Tests

- [`tests/conftest.py`](tests/conftest.py): cliente HTTP y overrides compartidos.
- [`tests/ticket_service_fakes.py`](tests/ticket_service_fakes.py): sesiones y
  queries simuladas para tests unitarios.
- [`tests/test_auth.py`](tests/test_auth.py): autenticación.
- [`tests/test_user_service.py`](tests/test_user_service.py): creación de usuarios.
- [`tests/test_category_service.py`](tests/test_category_service.py): categorías y
  colas.
- [`tests/test_category_routes.py`](tests/test_category_routes.py): contrato HTTP
  de configuración de routing de categorías.
- [`tests/test_team_queries.py`](tests/test_team_queries.py): membresía y liderazgo.
- [`tests/test_team_service.py`](tests/test_team_service.py): casos de uso de teams.
- [`tests/test_team_routes.py`](tests/test_team_routes.py): contrato HTTP de teams.
- [`tests/test_ticket_rules.py`](tests/test_ticket_rules.py): reglas puras.
- [`tests/test_ticket_service.py`](tests/test_ticket_service.py): creación y
  consultas principales.
- [`tests/test_ticket_assignment_service.py`](tests/test_ticket_assignment_service.py):
  asignaciones, teams y categorías.
- [`tests/test_ticket_lifecycle_service.py`](tests/test_ticket_lifecycle_service.py):
  estados y archivado.
- [`tests/test_ticket_dependency_service.py`](tests/test_ticket_dependency_service.py):
  dependencias.
- [`tests/test_ticket_comment_service.py`](tests/test_ticket_comment_service.py):
  visibilidad de comentarios.
- [`tests/test_ticket_auto_assignment_service.py`](tests/test_ticket_auto_assignment_service.py):
  estrategias y desempates del selector automático.
- [`tests/test_ticket_routes.py`](tests/test_ticket_routes.py): contrato HTTP de
  tickets con `TestClient`.
- [`tests/integration/conftest.py`](tests/integration/conftest.py): PostgreSQL y
  Alembic aislados para integración.
- [`tests/integration/test_ticket_workflows.py`](tests/integration/test_ticket_workflows.py):
  flujos completos con persistencia real.
- [`tests/README.md`](tests/README.md): estrategia y comandos de la suite.
- [`pytest.ini`](pytest.ini): configuración y markers de pytest.

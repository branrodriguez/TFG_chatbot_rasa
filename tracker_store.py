import contextlib
import itertools
import json
import logging
import os
import pickle
from datetime import datetime, timezone

from time import sleep
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    Iterator,
    List,
    Optional,
    Text,
    Union,
    TYPE_CHECKING,
    Generator,
)

from sqlalchemy.sql.expression import false

from boto3.dynamodb.conditions import Key
from pymongo.collection import Collection

import rasa.core.utils as core_utils
import rasa.shared.utils.cli
import rasa.shared.utils.common
import rasa.shared.utils.io
from rasa.shared.core.constants import ACTION_LISTEN_NAME
from rasa.core.brokers.broker import EventBroker
from rasa.core.constants import (
    POSTGRESQL_SCHEMA,
    POSTGRESQL_MAX_OVERFLOW,
    POSTGRESQL_POOL_SIZE,
)
from rasa.shared.core.conversation import Dialogue
from rasa.shared.core.domain import Domain
from rasa.shared.core.events import SessionStarted
from rasa.shared.core.trackers import (
    ActionExecuted,
    DialogueStateTracker,
    EventVerbosity,
)
from rasa.shared.exceptions import ConnectionException
from rasa.shared.nlu.constants import INTENT_NAME_KEY
from rasa.utils.endpoints import EndpointConfig
import sqlalchemy as sa

if TYPE_CHECKING:
    import boto3.resources.factory.dynamodb.Table
    from sqlalchemy.engine.url import URL
    from sqlalchemy.engine.base import Engine
    from sqlalchemy.orm import Session, Query
    from sqlalchemy import Sequence

logger = logging.getLogger(__name__)

# default values of PostgreSQL pool size and max overflow
POSTGRESQL_DEFAULT_MAX_OVERFLOW = 100
POSTGRESQL_DEFAULT_POOL_SIZE = 50

class TrackerStore:
    """Represents common behavior and interface for all `TrackerStore`s."""

    def __init__(
        self,
        domain: Optional[Domain],
        event_broker: Optional[EventBroker] = None,
        **kwargs: Dict[Text, Any],
    ) -> None:
        """Create a TrackerStore.

        Args:
            domain: The `Domain` to initialize the `DialogueStateTracker`.
            event_broker: An event broker to publish any new events to another
                destination.
            kwargs: Additional kwargs.
        """
        self.domain = domain
        self.event_broker = event_broker
        self.max_event_history = None

        # TODO: Remove this in Rasa Open Source 3.0
        self.retrieve_events_from_previous_conversation_sessions: Optional[bool] = None
        self._set_deprecated_kwargs_and_emit_warning(kwargs)

    def _set_deprecated_kwargs_and_emit_warning(self, kwargs: Dict[Text, Any]) -> None:
        retrieve_events_from_previous_conversation_sessions = kwargs.get(
            "retrieve_events_from_previous_conversation_sessions"
        )

        if retrieve_events_from_previous_conversation_sessions is not None:
            rasa.shared.utils.io.raise_deprecation_warning(
                f"Specifying the `retrieve_events_from_previous_conversation_sessions` "
                f"kwarg for the `{self.__class__.__name__}` class is deprecated and "
                f"will be removed in Rasa Open Source 3.0. "
                f"Please use the `retrieve_full_tracker()` method instead."
            )
            self.retrieve_events_from_previous_conversation_sessions = (
                retrieve_events_from_previous_conversation_sessions
            )

    @staticmethod
    def create(
        obj: Union["TrackerStore", EndpointConfig, None],
        domain: Optional[Domain] = None,
        event_broker: Optional[EventBroker] = None,
    ) -> "TrackerStore":
        """Factory to create a tracker store."""
        if isinstance(obj, TrackerStore):
            return obj

        from botocore.exceptions import BotoCoreError
        import sqlalchemy.exc

        try:
            return _create_from_endpoint_config(obj, domain, event_broker)
        except (
            BotoCoreError,
            sqlalchemy.exc.OperationalError,
            ConnectionError,
        ) as error:
            raise ConnectionException(
                "Cannot connect to tracker store." + str(error)
            ) from error

    def get_or_create_tracker(
        self,
        sender_id: Text,
        max_event_history: Optional[int] = None,
        append_action_listen: bool = True,
    ) -> "DialogueStateTracker":
        """Returns tracker or creates one if the retrieval returns None.

        Args:
            sender_id: Conversation ID associated with the requested tracker.
            max_event_history: Value to update the tracker store's max event history to.
            append_action_listen: Whether or not to append an initial `action_listen`.
        """
        self.max_event_history = max_event_history

        tracker = self.retrieve(sender_id)

        if tracker is None:
            tracker = self.create_tracker(
                sender_id, append_action_listen=append_action_listen
            )

        return tracker

    def init_tracker(self, sender_id: Text) -> "DialogueStateTracker":
        """Returns a Dialogue State Tracker"""
        return DialogueStateTracker(
            sender_id,
            self.domain.slots if self.domain else None,
            max_event_history=self.max_event_history,
        )

    def create_tracker(
        self, sender_id: Text, append_action_listen: bool = True
    ) -> DialogueStateTracker:
        """Creates a new tracker for `sender_id`.

        The tracker begins with a `SessionStarted` event and is initially listening.

        Args:
            sender_id: Conversation ID associated with the tracker.
            append_action_listen: Whether or not to append an initial `action_listen`.

        Returns:
            The newly created tracker for `sender_id`.
        """
        tracker = self.init_tracker(sender_id)

        if append_action_listen:
            tracker.update(ActionExecuted(ACTION_LISTEN_NAME))

        self.save(tracker)

        return tracker

    def save(self, tracker: DialogueStateTracker) -> None:
        """Save method that will be overridden by specific tracker."""
        raise NotImplementedError()

    def exists(self, conversation_id: Text) -> bool:
        """Checks if tracker exists for the specified ID.

        This method may be overridden by the specific tracker store for
        faster implementations.

        Args:
            conversation_id: Conversation ID to check if the tracker exists.

        Returns:
            `True` if the tracker exists, `False` otherwise.
        """
        return self.retrieve(conversation_id) is not None

    def retrieve(self, sender_id: Text) -> Optional[DialogueStateTracker]:
        """Retrieves tracker for the latest conversation session.

        This method will be overridden by the specific tracker store.

        Args:
            sender_id: Conversation ID to fetch the tracker for.

        Returns:
            Tracker containing events from the latest conversation sessions.
        """
        raise NotImplementedError()

    def retrieve_full_tracker(
        self, conversation_id: Text
    ) -> Optional[DialogueStateTracker]:
        """Retrieve method for fetching all tracker events across conversation sessions
        that may be overridden by specific tracker.

        The default implementation uses `self.retrieve()`.

        Args:
            conversation_id: The conversation ID to retrieve the tracker for.

        Returns:
            The fetch tracker containing all events across session starts.
        """
        return self.retrieve(conversation_id)

    def stream_events(self, tracker: DialogueStateTracker) -> None:
        """Streams events to a message broker"""
        offset = self.number_of_existing_events(tracker.sender_id)
        events = tracker.events
        for event in list(itertools.islice(events, offset, len(events))):
            body = {"sender_id": tracker.sender_id}
            body.update(event.as_dict())
            self.event_broker.publish(body)

    def number_of_existing_events(self, sender_id: Text) -> int:
        """Return number of stored events for a given sender id."""
        old_tracker = self.retrieve(sender_id)

        return len(old_tracker.events) if old_tracker else 0

    def keys(self) -> Iterable[Text]:
        """Returns the set of values for the tracker store's primary key"""
        raise NotImplementedError()

    @staticmethod
    def serialise_tracker(tracker: DialogueStateTracker) -> Text:
        """Serializes the tracker, returns representation of the tracker."""
        dialogue = tracker.as_dialogue()

        return json.dumps(dialogue.as_dict())

    @staticmethod
    def _deserialize_dialogue_from_pickle(
        sender_id: Text, serialised_tracker: bytes
    ) -> Dialogue:
        # TODO: Remove in Rasa Open Source 3.0
        rasa.shared.utils.io.raise_deprecation_warning(
            f"Found pickled tracker for "
            f"conversation ID '{sender_id}'. Deserialization of pickled "
            f"trackers is deprecated and will be removed in Rasa Open Source 3.0. Rasa "
            f"will perform any future save operations of this tracker using json "
            f"serialisation."
        )

        return pickle.loads(serialised_tracker)

    def deserialise_tracker(
        self, sender_id: Text, serialised_tracker: Union[Text, bytes]
    ) -> Optional[DialogueStateTracker]:
        """Deserializes the tracker and returns it."""

        tracker = self.init_tracker(sender_id)

        try:
            dialogue = Dialogue.from_parameters(json.loads(serialised_tracker))
        except UnicodeDecodeError:
            dialogue = self._deserialize_dialogue_from_pickle(
                sender_id, serialised_tracker
            )

        tracker.recreate_from_dialogue(dialogue)

        return tracker

def _create_sequence(table_name: Text) -> "Sequence":
    """Creates a sequence object for a specific table name.

    If using Oracle you will need to create a sequence in your database,
    as described here: https://rasa.com/docs/rasa/tracker-stores#sqltrackerstore
    Args:
        table_name: The name of the table, which gets a Sequence assigned

    Returns: A `Sequence` object
    """

    from sqlalchemy.ext.declarative import declarative_base

    sequence_name = f"{table_name}_seq"
    Base = declarative_base()
    return sa.Sequence(sequence_name, metadata=Base.metadata, optional=True)


def is_postgresql_url(url: Union[Text, "URL"]) -> bool:
    """Determine whether `url` configures a PostgreSQL connection.

    Args:
        url: SQL connection URL.

    Returns:
        `True` if `url` is a PostgreSQL connection URL.
    """
    if isinstance(url, str):
        return "postgresql" in url

    return url.drivername == "postgresql"


def create_engine_kwargs(url: Union[Text, "URL"]) -> Dict[Text, Any]:
    """Get `sqlalchemy.create_engine()` kwargs.

    Args:
        url: SQL connection URL.

    Returns:
        kwargs to be passed into `sqlalchemy.create_engine()`.
    """
    if not is_postgresql_url(url):
        return {}

    kwargs = {}

    schema_name = os.environ.get(POSTGRESQL_SCHEMA)

    if schema_name:
        logger.debug(f"Using PostgreSQL schema '{schema_name}'.")
        kwargs["connect_args"] = {"options": f"-csearch_path={schema_name}"}

    # pool_size and max_overflow can be set to control the number of
    # connections that are kept in the connection pool. Not available
    # for SQLite, and only  tested for PostgreSQL. See
    # https://docs.sqlalchemy.org/en/13/core/pooling.html#sqlalchemy.pool.QueuePool
    kwargs["pool_size"] = int(
        os.environ.get(POSTGRESQL_POOL_SIZE, POSTGRESQL_DEFAULT_POOL_SIZE)
    )
    kwargs["max_overflow"] = int(
        os.environ.get(POSTGRESQL_MAX_OVERFLOW, POSTGRESQL_DEFAULT_MAX_OVERFLOW)
    )

    return kwargs


def ensure_schema_exists(session: "Session") -> None:
    """Ensure that the requested PostgreSQL schema exists in the database.

    Args:
        session: Session used to inspect the database.

    Raises:
        `ValueError` if the requested schema does not exist.
    """
    schema_name = os.environ.get(POSTGRESQL_SCHEMA)

    if not schema_name:
        return

    engine = session.get_bind()

    if is_postgresql_url(engine.url):
        query = sa.exists(
            sa.select([(sa.text("schema_name"))])
            .select_from(sa.text("information_schema.schemata"))
            .where(sa.text(f"schema_name = '{schema_name}'"))
        )
        if not session.query(query).scalar():
            raise ValueError(schema_name)

class InMemoryTrackerStore(TrackerStore):
    """Stores conversation history in memory"""

    def __init__(
        self,
        domain: Domain,
        event_broker: Optional[EventBroker] = None,
        **kwargs: Dict[Text, Any],
    ) -> None:
        self.store = {}
        super().__init__(domain, event_broker, **kwargs)

    def save(self, tracker: DialogueStateTracker) -> None:
        """Updates and saves the current conversation state"""
        if self.event_broker:
            self.stream_events(tracker)
        serialised = InMemoryTrackerStore.serialise_tracker(tracker)
        self.store[tracker.sender_id] = serialised

    def retrieve(self, sender_id: Text) -> Optional[DialogueStateTracker]:
        if sender_id in self.store:
            logger.debug(f"Recreating tracker for id '{sender_id}'")
            return self.deserialise_tracker(sender_id, self.store[sender_id])

        logger.debug(f"Could not find tracker for conversation ID '{sender_id}'.")

        return None

    def keys(self) -> Iterable[Text]:
        """Returns sender_ids of the Tracker Store in memory"""
        return self.store.keys()





class SQLTrackerStore(TrackerStore):
    """Store which can save and retrieve trackers from an SQL database."""

    from sqlalchemy.ext.declarative import declarative_base, DeclarativeMeta

    Base: DeclarativeMeta = declarative_base()

    class SQLEvent(Base):
        """Represents an event in the SQL Tracker Store."""

        __tablename__ = "events"

        # `create_sequence` is needed to create a sequence for databases that
        # don't autoincrement Integer primary keys (e.g. Oracle)
        id = sa.Column(sa.Integer, _create_sequence(__tablename__), primary_key=True)
        sender_id = sa.Column(sa.String(255), nullable=False, index=True)
        type_name = sa.Column(sa.String(255), nullable=False)
        timestamp = sa.Column(sa.types.DateTime())
        intent_name = sa.Column(sa.String(255))
        action_name = sa.Column(sa.String(255))
        data = sa.Column(sa.Text)

    def __init__(
        self,
        domain: Optional[Domain] = None,
        dialect: Text = "sqlite",
        host: Optional[Text] = None,
        port: Optional[int] = None,
        db: Text = "rasa.db",
        username: Text = None,
        password: Text = None,
        event_broker: Optional[EventBroker] = None,
        login_db: Optional[Text] = None,
        query: Optional[Dict] = None,
        **kwargs: Dict[Text, Any],
    ) -> None:
        import sqlalchemy.exc

        engine_url = self.get_db_url(
            dialect, host, port, db, username, password, login_db, query
        )

        self.engine = sa.create_engine(engine_url, **create_engine_kwargs(engine_url))

        logger.debug(
            f"Attempting to connect to database via '{repr(self.engine.url)}'."
        )

        # Database might take a while to come up
        while True:
            try:
                # if `login_db` has been provided, use current channel with
                # that database to create working database `db`
                if login_db:
                    self._create_database_and_update_engine(db, engine_url)

                try:
                    self.Base.metadata.create_all(self.engine)
                except (
                    sqlalchemy.exc.OperationalError,
                    sqlalchemy.exc.ProgrammingError,
                ) as e:
                    # Several Rasa services started in parallel may attempt to
                    # create tables at the same time. That is okay so long as
                    # the first services finishes the table creation.
                    logger.error(f"Could not create tables: {e}")

                self.sessionmaker = sa.orm.session.sessionmaker(bind=self.engine)
                break
            except (
                sqlalchemy.exc.OperationalError,
                sqlalchemy.exc.IntegrityError,
            ) as error:

                logger.warning(error)
                sleep(5)

        logger.debug(f"Connection to SQL database '{db}' successful.")

        super().__init__(domain, event_broker, **kwargs)

    @staticmethod
    def get_db_url(
        dialect: Text = "sqlite",
        host: Optional[Text] = None,
        port: Optional[int] = None,
        db: Text = "rasa.db",
        username: Text = None,
        password: Text = None,
        login_db: Optional[Text] = None,
        query: Optional[Dict] = None,
    ) -> Union[Text, "URL"]:
        """Build an SQLAlchemy `URL` object representing the parameters needed
        to connect to an SQL database.

        Args:
            dialect: SQL database type.
            host: Database network host.
            port: Database network port.
            db: Database name.
            username: User name to use when connecting to the database.
            password: Password for database user.
            login_db: Alternative database name to which initially connect, and create
                the database specified by `db` (PostgreSQL only).
            query: Dictionary of options to be passed to the dialect and/or the
                DBAPI upon connect.

        Returns:
            URL ready to be used with an SQLAlchemy `Engine` object.
        """
        from urllib import parse

        # Users might specify a url in the host
        if host and "://" in host:
            # assumes this is a complete database host name including
            # e.g. `postgres://...`
            return host
        elif host:
            # add fake scheme to properly parse components
            parsed = parse.urlsplit(f"scheme://{host}")

            # users might include the port in the url
            port = parsed.port or port
            host = parsed.hostname or host

        return sa.engine.url.URL(
            dialect,
            username,
            password,
            host,
            port,
            database=login_db if login_db else db,
            query=query,
        )

    def _create_database_and_update_engine(self, db: Text, engine_url: "URL") -> None:
        """Creates database `db` and updates engine accordingly."""
        from sqlalchemy import create_engine

        if not self.engine.dialect.name == "postgresql":
            rasa.shared.utils.io.raise_warning(
                "The parameter 'login_db' can only be used with a postgres database.",
            )
            return

        self._create_database(self.engine, db)
        self.engine.dispose()
        engine_url = sa.engine.url.URL(
            drivername=engine_url.drivername,
            username=engine_url.username,
            password=engine_url.password,
            host=engine_url.host,
            port=engine_url.port,
            database=db,
            query=engine_url.query,
        )
        self.engine = create_engine(engine_url)

    @staticmethod
    def _create_database(engine: "Engine", database_name: Text) -> None:
        """Create database `db` on `engine` if it does not exist."""
        import sqlalchemy.exc

        conn = engine.connect()

        matching_rows = (
            conn.execution_options(isolation_level="AUTOCOMMIT")
            .execute(
                sa.text(
                    "SELECT 1 FROM pg_catalog.pg_database "
                    "WHERE datname = :database_name"
                ),
                database_name=database_name,
            )
            .rowcount
        )

        if not matching_rows:
            try:
                conn.execute(f"CREATE DATABASE {database_name}")
            except (
                sqlalchemy.exc.ProgrammingError,
                sqlalchemy.exc.IntegrityError,
            ) as e:
                logger.error(f"Could not create database '{database_name}': {e}")

        conn.close()

    @contextlib.contextmanager
    def session_scope(self) -> Generator["Session", None, None]:
        """Provide a transactional scope around a series of operations."""
        session = self.sessionmaker()
        try:
            ensure_schema_exists(session)
            yield session
        except ValueError as e:
            rasa.shared.utils.cli.print_error_and_exit(
                f"Requested PostgreSQL schema '{e}' was not found in the database. To "
                f"continue, please create the schema by running 'CREATE DATABASE {e};' "
                f"or unset the '{POSTGRESQL_SCHEMA}' environment variable in order to "
                f"use the default schema. Exiting application."
            )
        finally:
            session.close()

    def keys(self) -> Iterable[Text]:
        """Returns sender_ids of the SQLTrackerStore"""
        with self.session_scope() as session:
            sender_ids = session.query(self.SQLEvent.sender_id).distinct().all()
            return [sender_id for (sender_id,) in sender_ids]

    def retrieve(self, sender_id: Text) -> Optional[DialogueStateTracker]:
        # TODO: Remove this in Rasa Open Source 3.0 along with the
        # deprecation warning in the constructor
        if self.retrieve_events_from_previous_conversation_sessions:
            return self.retrieve_full_tracker(sender_id)

        return self._retrieve(sender_id, fetch_events_from_all_sessions=False)

    def retrieve_full_tracker(
        self, conversation_id: Text
    ) -> Optional[DialogueStateTracker]:
        return self._retrieve(conversation_id, fetch_events_from_all_sessions=True)

    def _retrieve(
        self, sender_id: Text, fetch_events_from_all_sessions: bool
    ) -> Optional[DialogueStateTracker]:
        with self.session_scope() as session:

            serialised_events = self._event_query(
                session,
                sender_id,
                fetch_events_from_all_sessions=fetch_events_from_all_sessions,
            ).all()

            events = [json.loads(event.data) for event in serialised_events]

            if self.domain and len(events) > 0:
                logger.debug(f"Recreating tracker from sender id '{sender_id}'")
                return DialogueStateTracker.from_dict(
                    sender_id, events, self.domain.slots
                )
            else:
                logger.debug(
                    f"Can't retrieve tracker matching "
                    f"sender id '{sender_id}' from SQL storage. "
                    f"Returning `None` instead."
                )
                return None

    def _event_query(
        self, session: "Session", sender_id: Text, fetch_events_from_all_sessions: bool
    ) -> "Query":
        """Provide the query to retrieve the conversation events for a specific sender.

        Args:
            session: Current database session.
            sender_id: Sender id whose conversation events should be retrieved.
            fetch_events_from_all_sessions: Whether to fetch events from all
                conversation sessions. If `False`, only fetch events from the
                latest conversation session.

        Returns:
            Query to get the conversation events.
        """
        # Subquery to find the timestamp of the latest `SessionStarted` event
        session_start_sub_query = (
            session.query(sa.func.max(self.SQLEvent.timestamp).label("session_start"))
            .filter(
                self.SQLEvent.sender_id == sender_id,
                self.SQLEvent.type_name == SessionStarted.type_name,
            )
            .subquery()
        )

        event_query = session.query(self.SQLEvent).filter(
            self.SQLEvent.sender_id == sender_id
        )
        if not fetch_events_from_all_sessions:
            event_query = event_query.filter(
                # Find events after the latest `SessionStarted` event or return all
                # events
                sa.or_(
                    self.SQLEvent.timestamp >= session_start_sub_query.c.session_start,
                    session_start_sub_query.c.session_start.is_(None),
                )
            )

        return event_query.order_by(self.SQLEvent.timestamp)

    def save(self, tracker: DialogueStateTracker) -> None:
        """Update database with events from the current conversation."""

        if self.event_broker:
            self.stream_events(tracker)

        with self.session_scope() as session:
            # only store recent events
            events = self._additional_events(session, tracker)

            for event in events:
                data = event.as_dict()
                intent = (
                    data.get("parse_data", {}).get("intent", {}).get(INTENT_NAME_KEY)
                )
                action = data.get("name")
                timestamp = datetime.now(timezone.utc)

                # noinspection PyArgumentList
                session.add(
                    self.SQLEvent(
                        sender_id=tracker.sender_id,
                        type_name=event.type_name,
                        timestamp=timestamp,
                        intent_name=intent,
                        action_name=action,
                        data=json.dumps(data),
                    )
                )
            session.commit()

        logger.debug(f"Tracker with sender_id '{tracker.sender_id}' stored to database")

    def _additional_events(
        self, session: "Session", tracker: DialogueStateTracker
    ) -> Iterator:
        """Return events from the tracker which aren't currently stored."""
        number_of_events_since_last_session = self._event_query(
            session,
            tracker.sender_id,
            fetch_events_from_all_sessions=(
                self.retrieve_events_from_previous_conversation_sessions
            ),
        ).count()

        return itertools.islice(
            tracker.events, number_of_events_since_last_session, len(tracker.events)
        )


class FailSafeTrackerStore(TrackerStore):
    """Wraps a tracker store so that we can fallback to a different tracker store in
    case of errors."""

    def __init__(
        self,
        tracker_store: TrackerStore,
        on_tracker_store_error: Optional[Callable[[Exception], None]] = None,
        fallback_tracker_store: Optional[TrackerStore] = None,
    ) -> None:
        """Create a `FailSafeTrackerStore`.

        Args:
            tracker_store: Primary tracker store.
            on_tracker_store_error: Callback which is called when there is an error
                in the primary tracker store.
        """

        self._fallback_tracker_store: Optional[TrackerStore] = fallback_tracker_store
        self._tracker_store = tracker_store
        self._on_tracker_store_error = on_tracker_store_error

        super().__init__(tracker_store.domain, tracker_store.event_broker)

    @property
    def domain(self) -> Optional[Domain]:
        return self._tracker_store.domain

    @domain.setter
    def domain(self, domain: Optional[Domain]) -> None:
        self._tracker_store.domain = domain

        if self._fallback_tracker_store:
            self._fallback_tracker_store.domain = domain

    @property
    def fallback_tracker_store(self) -> TrackerStore:
        if not self._fallback_tracker_store:
            self._fallback_tracker_store = InMemoryTrackerStore(
                self._tracker_store.domain, self._tracker_store.event_broker
            )

        return self._fallback_tracker_store

    def on_tracker_store_error(self, error: Exception) -> None:
        if self._on_tracker_store_error:
            self._on_tracker_store_error(error)
        else:
            logger.error(
                f"Error happened when trying to save conversation tracker to "
                f"'{self._tracker_store.__class__.__name__}'. Falling back to use "
                f"the '{InMemoryTrackerStore.__name__}'. Please "
                f"investigate the following error: {error}."
            )

    def retrieve(self, sender_id: Text) -> Optional[DialogueStateTracker]:
        try:
            return self._tracker_store.retrieve(sender_id)
        except Exception as e:
            self.on_tracker_store_error(e)
            return None

    def keys(self) -> Iterable[Text]:
        try:
            return self._tracker_store.keys()
        except Exception as e:
            self.on_tracker_store_error(e)
            return []

    def save(self, tracker: DialogueStateTracker) -> None:
        try:
            self._tracker_store.save(tracker)
        except Exception as e:
            self.on_tracker_store_error(e)
            self.fallback_tracker_store.save(tracker)


def _create_from_endpoint_config(
    endpoint_config: Optional[EndpointConfig] = None,
    domain: Optional[Domain] = None,
    event_broker: Optional[EventBroker] = None,
) -> "TrackerStore":
    """Given an endpoint configuration, create a proper tracker store object."""

    domain = domain or Domain.empty()

    if endpoint_config is None or endpoint_config.type is None:
        # default tracker store if no type is set
        tracker_store = InMemoryTrackerStore(domain, event_broker)
    elif endpoint_config.type.lower() == "sql":
        tracker_store = SQLTrackerStore(
            domain=domain,
            host=endpoint_config.url,
            event_broker=event_broker,
            **endpoint_config.kwargs,
        )
    else:
        tracker_store = _load_from_module_name_in_endpoint_config(
            domain, endpoint_config, event_broker
        )

    logger.debug(f"Connected to {tracker_store.__class__.__name__}.")

    return tracker_store


def _load_from_module_name_in_endpoint_config(
    domain: Domain, store: EndpointConfig, event_broker: Optional[EventBroker] = None
) -> "TrackerStore":
    """Initializes a custom tracker.

    Defaults to the InMemoryTrackerStore if the module path can not be found.

    Args:
        domain: defines the universe in which the assistant operates
        store: the specific tracker store
        event_broker: an event broker to publish events

    Returns:
        a tracker store from a specified type in a stores endpoint configuration
    """

    try:
        tracker_store_class = rasa.shared.utils.common.class_from_module_path(
            store.type
        )

        return tracker_store_class(
            host=store.url, domain=domain, event_broker=event_broker, **store.kwargs
        )
    except (AttributeError, ImportError):
        rasa.shared.utils.io.raise_warning(
            f"Tracker store with type '{store.type}' not found. "
            f"Using `InMemoryTrackerStore` instead."
        )
        return InMemoryTrackerStore(domain)

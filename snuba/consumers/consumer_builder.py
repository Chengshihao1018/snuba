import functools
import logging
from dataclasses import dataclass
from typing import Callable, Optional, Sequence

from arroyo import Topic
from arroyo.backends.kafka import KafkaConsumer, KafkaPayload
from arroyo.commit import IMMEDIATE
from arroyo.processing import StreamProcessor
from arroyo.processing.strategies import ProcessingStrategyFactory
from arroyo.utils.profiler import ProcessingStrategyProfilerWrapperFactory
from arroyo.utils.retries import BasicRetryPolicy, RetryPolicy
from confluent_kafka import KafkaError, KafkaException, Producer

from snuba.consumers.consumer import (
    CommitLogConfig,
    build_batch_writer,
    process_message,
)
from snuba.consumers.strategy_factory import KafkaConsumerStrategyFactory
from snuba.datasets.slicing import validate_passed_slice
from snuba.datasets.storages.factory import get_writable_storage
from snuba.datasets.storages.storage_key import StorageKey
from snuba.environment import setup_sentry
from snuba.state import get_config
from snuba.utils.metrics import MetricsBackend
from snuba.utils.streams.configuration_builder import (
    build_kafka_consumer_configuration,
    build_kafka_producer_configuration,
    get_default_kafka_configuration,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class KafkaParameters:
    raw_topic: Optional[str]
    replacements_topic: Optional[str]
    bootstrap_servers: Optional[Sequence[str]]
    group_id: str
    commit_log_topic: Optional[str]
    auto_offset_reset: str
    strict_offset_reset: Optional[bool]
    queued_max_messages_kbytes: int
    queued_min_messages: int


@dataclass(frozen=True)
class ProcessingParameters:
    processes: Optional[int]
    input_block_size: Optional[int]
    output_block_size: Optional[int]


class ConsumerBuilder:
    """
    Simplifies the initialization of a consumer by merging parameters that
    generally come from the command line with defaults that come from the
    dataset class and defaults that come from the settings file.
    """

    def __init__(
        self,
        storage_key: StorageKey,
        kafka_params: KafkaParameters,
        processing_params: ProcessingParameters,
        max_batch_size: int,
        max_batch_time_ms: int,
        metrics: MetricsBackend,
        slice_id: Optional[int],
        stats_callback: Optional[Callable[[str], None]] = None,
        commit_retry_policy: Optional[RetryPolicy] = None,
        validate_schema: bool = False,
        profile_path: Optional[str] = None,
    ) -> None:
        self.storage = get_writable_storage(storage_key)
        self.bootstrap_servers = kafka_params.bootstrap_servers
        self.consumer_group = kafka_params.group_id
        topic = (
            self.storage.get_table_writer()
            .get_stream_loader()
            .get_default_topic_spec()
            .topic
        )

        # Ensure that the slice, storage set combination is valid
        validate_passed_slice(self.storage.get_storage_set_key(), slice_id)

        self.broker_config = get_default_kafka_configuration(
            topic, slice_id, bootstrap_servers=kafka_params.bootstrap_servers
        )
        logger.info(f"librdkafka log level: {self.broker_config.get('log_level', 6)}")
        self.producer_broker_config = build_kafka_producer_configuration(
            topic,
            slice_id,
            bootstrap_servers=kafka_params.bootstrap_servers,
            override_params={
                "partitioner": "consistent",
                "message.max.bytes": 50000000,  # 50MB, default is 1MB
            },
        )

        stream_loader = self.storage.get_table_writer().get_stream_loader()

        self.raw_topic: Topic
        if kafka_params.raw_topic is not None:
            self.raw_topic = Topic(kafka_params.raw_topic)
        else:
            default_topic_spec = stream_loader.get_default_topic_spec()
            self.raw_topic = Topic(default_topic_spec.get_physical_topic_name(slice_id))

        self.replacements_topic: Optional[Topic]
        if kafka_params.replacements_topic is not None:
            self.replacements_topic = Topic(kafka_params.replacements_topic)
        else:
            replacement_topic_spec = stream_loader.get_replacement_topic_spec()
            if replacement_topic_spec is not None:
                self.replacements_topic = Topic(
                    replacement_topic_spec.get_physical_topic_name(slice_id)
                )
            else:
                self.replacements_topic = None

        self.commit_log_topic: Optional[Topic]
        if kafka_params.commit_log_topic is not None:
            self.commit_log_topic = Topic(kafka_params.commit_log_topic)

        else:
            commit_log_topic_spec = stream_loader.get_commit_log_topic_spec()
            if commit_log_topic_spec is not None:
                self.commit_log_topic = Topic(
                    commit_log_topic_spec.get_physical_topic_name(slice_id)
                )
            else:
                self.commit_log_topic = None

        self.stats_callback = stats_callback

        # XXX: This can result in a producer being built in cases where it's
        # not actually required.
        self.producer = Producer(self.producer_broker_config)

        self.metrics = metrics
        self.max_batch_size = max_batch_size
        self.max_batch_time_ms = max_batch_time_ms
        self.group_id = kafka_params.group_id
        self.auto_offset_reset = kafka_params.auto_offset_reset
        self.strict_offset_reset = kafka_params.strict_offset_reset
        self.queued_max_messages_kbytes = kafka_params.queued_max_messages_kbytes
        self.queued_min_messages = kafka_params.queued_min_messages
        self.processes = processing_params.processes
        self.input_block_size = processing_params.input_block_size
        self.output_block_size = processing_params.output_block_size
        self.__profile_path = profile_path

        if commit_retry_policy is None:
            commit_retry_policy = BasicRetryPolicy(
                3,
                1,
                lambda e: isinstance(e, KafkaException)
                and e.args[0].code()
                in (
                    KafkaError.REQUEST_TIMED_OUT,
                    KafkaError.NOT_COORDINATOR,
                    KafkaError._WAIT_COORD,
                ),
            )

        self.__commit_retry_policy = commit_retry_policy
        self.__validate_schema = validate_schema

    def __build_consumer(
        self,
        strategy_factory: ProcessingStrategyFactory[KafkaPayload],
        slice_id: Optional[int] = None,
    ) -> StreamProcessor[KafkaPayload]:

        # retrieves the default logical topic
        topic = (
            self.storage.get_table_writer()
            .get_stream_loader()
            .get_default_topic_spec()
            .topic
        )

        configuration = build_kafka_consumer_configuration(
            topic,
            bootstrap_servers=self.bootstrap_servers,
            group_id=self.group_id,
            slice_id=slice_id,
            auto_offset_reset=self.auto_offset_reset,
            strict_offset_reset=self.strict_offset_reset,
            queued_max_messages_kbytes=self.queued_max_messages_kbytes,
            queued_min_messages=self.queued_min_messages,
        )

        stats_collection_frequency_ms = get_config(
            f"stats_collection_freq_ms_{self.group_id}",
            get_config("stats_collection_freq_ms", 0),
        )

        if stats_collection_frequency_ms and stats_collection_frequency_ms > 0:
            configuration.update(
                {
                    "statistics.interval.ms": stats_collection_frequency_ms,
                    "stats_cb": self.stats_callback,
                }
            )

        consumer = KafkaConsumer(
            configuration,
            commit_retry_policy=self.__commit_retry_policy,
        )

        return StreamProcessor(consumer, self.raw_topic, strategy_factory, IMMEDIATE)

    def __build_streaming_strategy_factory(
        self,
        slice_id: Optional[int] = None,
    ) -> ProcessingStrategyFactory[KafkaPayload]:
        table_writer = self.storage.get_table_writer()
        stream_loader = table_writer.get_stream_loader()

        logical_topic = stream_loader.get_default_topic_spec().topic

        processor = stream_loader.get_processor()

        if self.commit_log_topic:
            commit_log_config = CommitLogConfig(
                self.producer, self.commit_log_topic, self.group_id
            )
        else:
            commit_log_config = None

        strategy_factory: ProcessingStrategyFactory[
            KafkaPayload
        ] = KafkaConsumerStrategyFactory(
            prefilter=stream_loader.get_pre_filter(),
            process_message=functools.partial(
                process_message,
                processor,
                self.consumer_group,
                logical_topic,
                self.__validate_schema,
            ),
            collector=build_batch_writer(
                table_writer,
                metrics=self.metrics,
                replacements_producer=(
                    self.producer if self.replacements_topic is not None else None
                ),
                replacements_topic=self.replacements_topic,
                slice_id=slice_id,
                commit_log_config=commit_log_config,
            ),
            max_batch_size=self.max_batch_size,
            max_batch_time=self.max_batch_time_ms / 1000.0,
            processes=self.processes,
            input_block_size=self.input_block_size,
            output_block_size=self.output_block_size,
            initialize_parallel_transform=setup_sentry,
            dead_letter_queue_policy_creator=stream_loader.get_dead_letter_queue_policy_creator(),
        )

        if self.__profile_path is not None:
            strategy_factory = ProcessingStrategyProfilerWrapperFactory(
                strategy_factory,
                self.__profile_path,
            )

        return strategy_factory

    def build_base_consumer(
        self, slice_id: Optional[int] = None
    ) -> StreamProcessor[KafkaPayload]:
        """
        Builds the consumer.
        """
        return self.__build_consumer(
            self.__build_streaming_strategy_factory(slice_id), slice_id
        )
